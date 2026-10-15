"""Local UI API. SSE tails persisted observable events, not the job coroutine."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .files import safe_path
from .service import ACTIVE, FileAgentError, FileAgentService

router = APIRouter(prefix="/api/file-agent", tags=["file-agent"])


def service(request: Request) -> FileAgentService:
    return request.app.state.file_agent_service


Service = Annotated[FileAgentService, Depends(service)]


async def local_mutation(request: Request) -> None:
    """Block browser cross-site requests, including localhost CSRF form posts.

    No-Origin CLI requests remain supported. The two existing development UI
    origins are accepted, without expanding application CORS.
    """
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site file-agent mutation is forbidden.")
    origin = request.headers.get("origin")
    if origin:
        own = f"{request.url.scheme}://{request.url.netloc}"
        if origin not in {own, "http://localhost:3000", "http://127.0.0.1:3000"}:
            raise HTTPException(403, "Untrusted request origin.")
        parsed = urlsplit(origin)
        # Matching an attacker-controlled Host is insufficient under DNS
        # rebinding; browser mutations are limited to loopback UI authorities.
        if parsed.username or parsed.password or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "Untrusted request origin.")
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(415, "Mutating endpoints require application/json.")
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 100_000):
        raise HTTPException(413, "Request exceeds size limit.")


Mutation = [Depends(local_mutation)]


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:/-]+$")


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmRequest(EmptyRequest):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("Explicit boolean confirmation is required")
        return True


class ApplyRequest(ConfirmRequest):
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FileQuery(BaseModel):
    path: str = Field(min_length=1, max_length=2048)

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_path(value)


def fail(exc: Exception) -> HTTPException:
    if isinstance(exc, FileAgentError):
        return HTTPException(exc.status, str(exc))
    return HTTPException(409, "File-agent operation unavailable or conflicting. Inspect task history.")


@router.get("/tasks")
def tasks(svc: Service, limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    return svc.history(limit, offset)


@router.post("/tasks", status_code=202, dependencies=Mutation)
async def create(data: TaskCreate, svc: Service):
    try:
        return await svc.create(data.prompt, data.model)
    except Exception as exc:
        raise fail(exc) from None


@router.get("/tasks/{task_id}")
def task(task_id: str, svc: Service):
    try:
        return svc.serialize(svc.require(task_id))
    except Exception as exc:
        raise fail(exc) from None


@router.get("/tasks/{task_id}/diff")
async def diff(task_id: str, svc: Service, path: str | None = Query(default=None, max_length=2048)):
    try:
        if path is not None:
            safe_path(path)
        return await svc.inspect(task_id, "file_diff", path)
    except ValueError:
        raise HTTPException(400, "A permitted relative file path is required.") from None
    except Exception as exc:
        raise fail(exc) from None


@router.get("/tasks/{task_id}/file")
async def file(task_id: str, svc: Service, query: Annotated[FileQuery, Query()]):
    try:
        return await svc.inspect(task_id, "file_read", query.path)
    except Exception as exc:
        raise fail(exc) from None


@router.get("/tasks/{task_id}/patch")
async def patch(task_id: str, svc: Service):
    try:
        preview = await svc.inspect(task_id, "file_diff")
        item = svc.require(task_id)
        provenance = (f"# REDACTED PREVIEW ONLY; NOT AN EXACT APPLY ARTIFACT\n# task: {item.id}\n"
                      f"# base: {item.base_sha}\n# private artifact SHA-256: {item.workspace.get('artifact', {}).get('sha256', 'unavailable')}\n"
                      f"# truncated: {preview['truncated']}\n")
        return Response(provenance + preview["diff"], media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="file-agent-{item.id}.redacted.patch"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except Exception as exc:
        raise fail(exc) from None


@router.get("/tasks/{task_id}/events")
async def events(task_id: str, request: Request, svc: Service, after: int = Query(default=0, ge=0)):
    try:
        svc.require(task_id)
    except Exception as exc:
        raise fail(exc) from None
    last = request.headers.get("last-event-id", "")
    if last.isdigit():
        after = max(after, int(last))

    async def stream():
        cursor = after
        heartbeats = 0
        while not await request.is_disconnected():
            try:
                item = svc.require(task_id)
            except FileAgentError as exc:
                if exc.status == 404:
                    yield 'event: deleted\ndata: {}\n\n'
                    return
                raise
            for event in item.operations:
                if event["id"] > cursor:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: operation\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if item.status not in ACTIVE:
                payload = json.dumps(jsonable_encoder(svc.serialize(item)), ensure_ascii=False)
                yield f"event: done\ndata: {payload}\n\n"
                return
            heartbeats += 1
            if heartbeats % 30 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no",
    })


@router.post("/tasks/{task_id}/rerun", status_code=202, dependencies=Mutation)
async def rerun(task_id: str, data: EmptyRequest, svc: Service):
    try:
        return await svc.rerun(task_id)
    except Exception as exc:
        raise fail(exc) from None


@router.post("/tasks/{task_id}/replay", status_code=202, dependencies=Mutation)
async def replay(task_id: str, data: EmptyRequest, svc: Service):
    try:
        return await svc.rerun(task_id, replay=True)
    except Exception as exc:
        raise fail(exc) from None


@router.post("/tasks/{task_id}/apply", dependencies=Mutation)
async def apply(task_id: str, data: ApplyRequest, svc: Service):
    try:
        return await svc.lifecycle(task_id, "apply", data.artifact_sha256)
    except Exception as exc:
        raise fail(exc) from None


@router.post("/tasks/{task_id}/discard", dependencies=Mutation)
async def discard(task_id: str, data: ConfirmRequest, svc: Service):
    try:
        return await svc.lifecycle(task_id, "discard")
    except Exception as exc:
        raise fail(exc) from None


@router.post("/tasks/{task_id}/stop", status_code=202, dependencies=Mutation)
async def stop(task_id: str, data: EmptyRequest, svc: Service):
    try:
        return await svc.stop(task_id)
    except Exception as exc:
        raise fail(exc) from None


@router.delete("/tasks/{task_id}", status_code=204, dependencies=Mutation)
async def delete_history(task_id: str, data: ConfirmRequest, svc: Service):
    try:
        await svc.delete_history(task_id)
        return Response(status_code=204)
    except Exception as exc:
        raise fail(exc) from None
