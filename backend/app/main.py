from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .agent import AgentOrchestrator, DeepSeekClient
from .agent.orchestrator import _event
from .config import get_settings
from .crud import ConversationNotFoundError, ConversationService, RagService
from .database import SessionLocal, get_db, init_db
from .file_agent.router import router as file_agent_router
from .file_agent.service import FileAgentService
from .mcp_tools import GitMcpClient
from .models import MessageRole
from .rag import HybridRetriever, RagIndexer
from .review.engine import CodeReviewAgent, ReviewRAGService, parse_review_command
from .review.formatter import ReviewReportFormatter
from .review.github import GitHubApiError, GitHubPRClient, GitHubReviewPublisher
from .review.schemas import PullRequestData
from .review.service import (
    ActiveReviewError,
    ReviewHistoryService,
    ReviewNotFoundError,
    ReviewSyncService,
)
from .schemas import (
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageCreate,
)

logger = logging.getLogger(__name__)
settings = get_settings()
llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url)
_index_lock = threading.Lock()
_review_semaphore = asyncio.Semaphore(settings.review_max_parallel)
_review_jobs: set[asyncio.Task[Any]] = set()
_review_formatter = ReviewReportFormatter()


def _run_index() -> None:
    if not _index_lock.acquire(blocking=False):
        return
    try:
        with SessionLocal() as db:
            rag = RagService(db)
            rag.update_index_state(
                status="indexing",
                repository_path=settings.target_repo_path,
                started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                last_error=None,
            )
        RagIndexer(SessionLocal, settings.target_repo_path).index()
    except Exception as exc:
        logger.exception("RAG indexing failed")
        with SessionLocal() as db:
            RagService(db).update_index_state(status="error", last_error=str(exc)[:2_000])
    finally:
        _index_lock.release()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    app.state.file_agent_service.recover()
    repo = Path(settings.target_repo_path)
    if settings.auto_index and repo.is_dir():
        asyncio.create_task(asyncio.to_thread(_run_index))
    try:
        yield
    finally:
        await app.state.file_agent_service.close()


app = FastAPI(
    title="Developer AI Assistant API",
    version="2.0.0",
    description="DeepSeek agent with repository RAG and read-only Git MCP tools.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.file_agent_service = FileAgentService(settings, llm, SessionLocal)
app.include_router(file_agent_router)

Db = Annotated[Session, Depends(get_db)]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def _service(db: Session) -> ConversationService:
    return ConversationService(db)


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Диалог не найден: {exc}")


def _message_payload(message: Any) -> dict[str, Any]:
    return AgentOrchestrator._message_payload(message)


def _github() -> GitHubPRClient:
    return GitHubPRClient(settings.github_token, settings.github_repository)


def _review_agent(db: Session) -> CodeReviewAgent:
    return CodeReviewAgent(
        settings,
        llm,
        ReviewRAGService(db, HybridRetriever(db), settings),
        GitMcpClient(settings.target_repo_path) if Path(settings.target_repo_path).is_dir() else None,
    )


def _pr_payload(pr: Any) -> dict[str, Any]:
    reviews = sorted(pr.reviews, key=lambda item: item.created_at, reverse=True)
    latest = reviews[0] if reviews else None
    review_status = "not_reviewed"
    if latest:
        review_status = "outdated" if latest.reviewed_head_sha != pr.head_sha else latest.status
    return {
        "id": pr.id,
        "repository": pr.repository,
        "number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "base_branch": pr.base_branch,
        "head_branch": pr.head_branch,
        "base_sha": pr.base_sha,
        "head_sha": pr.head_sha,
        "state": pr.state,
        "html_url": pr.html_url,
        "github_updated_at": pr.github_updated_at,
        "updated_at": pr.updated_at,
        "review_status": review_status,
        "last_review": ReviewHistoryService.serialize(latest) if latest else None,
    }


async def _execute_review(review_id: str, pull_request: PullRequestData) -> None:
    async with _review_semaphore:
        with SessionLocal() as db:
            history = ReviewHistoryService(db)
            try:
                history.mark_running(review_id)
                report = await _review_agent(db).review(pull_request)
                github = _github()
                try:
                    comment = await GitHubReviewPublisher(github, _review_formatter).publish(report)
                finally:
                    await github.close()
                history.complete(review_id, report, github_comment=comment)
            except Exception as exc:
                logger.exception("Review job failed for %s", review_id)
                history.fail(review_id, str(exc))


def _start_review_job(review_id: str, pull_request: PullRequestData) -> None:
    task = asyncio.create_task(_execute_review(review_id, pull_request))
    _review_jobs.add(task)
    task.add_done_callback(_review_jobs.discard)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "deepseek_configured": llm.configured,
        "repository_available": Path(settings.target_repo_path).is_dir(),
    }


@app.get("/api/models")
async def models() -> dict[str, Any]:
    fallback = [
        {"id": "deepseek-flash", "name": "DeepSeek Flash"},
        {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    ]
    try:
        return {"models": await llm.models(), "source": "provider"}
    except Exception as exc:
        return {"models": fallback, "source": "fallback", "warning": str(exc)[:300]}


@app.post("/api/conversations", response_model=ConversationRead, status_code=201)
def create_conversation(data: ConversationCreate, db: Db) -> Any:
    return _service(db).create(data)


@app.get("/api/conversations", response_model=list[ConversationRead])
def list_conversations(db: Db) -> Any:
    return _service(db).list()


@app.get("/api/conversations/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: str, db: Db) -> Any:
    try:
        return _service(db).require(conversation_id)
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc


@app.patch("/api/conversations/{conversation_id}", response_model=ConversationRead)
def update_conversation(conversation_id: str, data: ConversationUpdate, db: Db) -> Any:
    try:
        return _service(db).update(conversation_id, data)
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, db: Db) -> Response:
    if not _service(db).delete(conversation_id):
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str, db: Db) -> list[dict[str, Any]]:
    try:
        return [_message_payload(item) for item in _service(db).list_messages(conversation_id)]
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc


@app.get("/api/conversations/{conversation_id}/summary")
def conversation_summary(conversation_id: str, db: Db) -> dict[str, Any]:
    try:
        conversation = _service(db).require(conversation_id)
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc
    return {
        "summary": conversation.summary or "",
        "message_count": conversation.user_message_count,
        "updated_at": conversation.summary_updated_at,
    }


@app.get("/api/conversations/{conversation_id}/usage")
def conversation_usage(conversation_id: str, db: Db) -> Any:
    try:
        return _service(db).usage(conversation_id)
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc


@app.post("/api/conversations/{conversation_id}/chat")
async def chat(conversation_id: str, data: ChatRequest, db: Db) -> StreamingResponse:
    try:
        _service(db).require(conversation_id)
    except ConversationNotFoundError as exc:
        raise _not_found(exc) from exc
    repo = Path(settings.target_repo_path)
    if not repo.is_dir():
        raise HTTPException(status_code=503, detail="TARGET_REPO_PATH недоступен")
    review_number = parse_review_command(data.message, settings.github_repository)
    history = ReviewHistoryService(db)
    active_review = history.conversation_review(conversation_id)
    review_context = None
    if active_review and active_review.status == "completed":
        serialized = history.serialize(active_review)
        review_context = {
            "review_id": active_review.id,
            "repository": serialized.get("repository"),
            "pr_number": serialized.get("pr_number"),
            "head_sha": active_review.reviewed_head_sha,
            "summary": active_review.summary,
            "findings": serialized.get("findings", [])[:20],
        }
    orchestrator = AgentOrchestrator(
        db,
        settings,
        llm,
        HybridRetriever(db),
        GitMcpClient(repo),
        review_context=review_context,
    )

    async def events():
        try:
            if review_number:
                number = review_number
                service = ConversationService(db)
                conversation = service.require(conversation_id)
                service.add_message(
                    conversation_id, MessageCreate(role=MessageRole.USER, content=data.message.strip())
                )
                yield _event("status", {"type": "status", "stage": "Getting PR"})
                github = _github()
                try:
                    pull_request = await github.get_pull_request(number)
                finally:
                    await github.close()
                yield _event("status", {"type": "status", "stage": "Fetching diff"})
                pr = history.upsert_pull_request(pull_request)
                try:
                    review = history.create_review(
                        pr,
                        source="manual",
                        model=settings.review_model or conversation.model,
                    )
                except ActiveReviewError as exc:
                    review = history.require(str(exc))
                history.mark_running(review.id)
                history.link_conversation(conversation_id, review.id)
                yield _event("status", {"type": "status", "stage": "Retrieving project context"})
                yield _event("status", {"type": "status", "stage": "AI analysis"})
                try:
                    report = await _review_agent(db).review(pull_request, model=conversation.model)
                    history.complete(review.id, report)
                except Exception as exc:
                    history.fail(review.id, str(exc))
                    raise
                yield _event("status", {"type": "status", "stage": "Finalizing review"})
                content = _review_formatter.markdown(report)
                assistant = service.add_message(
                    conversation_id,
                    MessageCreate(
                        role=MessageRole.ASSISTANT,
                        content=content,
                        model=report.model,
                        input_tokens=report.prompt_tokens,
                        output_tokens=report.completion_tokens,
                        total_tokens=report.total_tokens,
                        tokens_estimated=False,
                        total_ms=report.elapsed_ms,
                        llm_ms=report.elapsed_ms,
                        sources=[item.model_dump(exclude={"content"}) for item in report.sources],
                        rag_chunks=len(report.sources),
                        tools=[{"name": "github_pull_request", "status": "success", "result": {"number": number}}],
                    ),
                )
                yield _event("delta", {"type": "delta", "delta": content})
                yield _event(
                    "metrics",
                    {
                        "type": "metrics",
                        "metrics": {
                            "model": report.model,
                            "input_tokens": report.prompt_tokens,
                            "output_tokens": report.completion_tokens,
                            "total_tokens": report.total_tokens,
                            "latency_ms": report.elapsed_ms,
                            "rag_chunks": len(report.sources),
                        },
                    },
                )
                yield _event("done", {"type": "done", "message": _message_payload(assistant)})
            else:
                async for item in orchestrator.chat(conversation_id, data.message):
                    yield item
        except Exception as exc:
            logger.exception("Agent request failed")
            payload = json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/repository")
async def repository() -> Any:
    try:
        return await GitMcpClient(settings.target_repo_path).call("git_repo_info")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/pull-requests")
async def pull_requests(db: Db) -> list[dict[str, Any]]:
    github = _github()
    try:
        for item in await github.list_pull_requests("open"):
            ReviewHistoryService(db).upsert_pull_request(item)
    except GitHubApiError as exc:
        if not ReviewHistoryService(db).list_pull_requests():
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        logger.warning("Using cached pull requests after GitHub error: %s", exc)
    finally:
        await github.close()
    return [_pr_payload(item) for item in ReviewHistoryService(db).list_pull_requests()]


@app.get("/api/pull-requests/{number}")
async def pull_request(number: int, db: Db) -> dict[str, Any]:
    github = _github()
    try:
        data = await github.get_pull_request(number)
    except GitHubApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        await github.close()
    item = ReviewHistoryService(db).upsert_pull_request(data)
    payload = _pr_payload(item)
    payload["files"] = [file.model_dump() for file in data.files]
    return payload


@app.get("/api/pull-requests/{number}/reviews")
def pull_request_reviews(number: int, db: Db) -> list[dict[str, Any]]:
    return [
        ReviewHistoryService.serialize(item)
        for item in ReviewHistoryService(db).list()
        if item.pull_request.number == number
    ]


@app.post("/api/pull-requests/{number}/review", status_code=202)
async def run_pull_request_review(number: int, db: Db) -> dict[str, Any]:
    github = _github()
    try:
        data = await github.get_pull_request(number)
    except GitHubApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        await github.close()
    history = ReviewHistoryService(db)
    pr = history.upsert_pull_request(data)
    try:
        review = history.create_review(
            pr, source="manual", model=settings.review_model or settings.default_model
        )
    except ActiveReviewError as exc:
        review = history.require(str(exc))
        return history.serialize(review)
    _start_review_job(review.id, data)
    return history.serialize(review)


@app.get("/api/reviews")
def reviews(db: Db) -> list[dict[str, Any]]:
    return [ReviewHistoryService.serialize(item) for item in ReviewHistoryService(db).list()]


@app.get("/api/reviews/{review_id}")
def review(review_id: str, db: Db) -> dict[str, Any]:
    try:
        return ReviewHistoryService.serialize(ReviewHistoryService(db).require(review_id))
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found") from exc


@app.get("/api/reviews/{review_id}/findings")
def review_findings(review_id: str, db: Db) -> list[dict[str, Any]]:
    try:
        item = ReviewHistoryService(db).require(review_id)
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found") from exc
    return ReviewHistoryService.serialize(item)["findings"]


@app.post("/api/reviews/{review_id}/discuss", status_code=201)
def discuss_review(review_id: str, db: Db) -> dict[str, str]:
    history = ReviewHistoryService(db)
    try:
        review_item = history.require(review_id)
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review not found") from exc
    conversation = ConversationService(db).create(
        ConversationCreate(title=f"Review PR #{review_item.pull_request.number}")
    )
    history.link_conversation(conversation.id, review_item.id)
    return {"conversation_id": conversation.id, "review_id": review_item.id}


@app.post("/api/reviews/sync")
async def sync_reviews(db: Db) -> dict[str, Any]:
    github = _github()
    try:
        return await ReviewSyncService(db, github).sync()
    except GitHubApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        await github.close()


@app.get("/api/mcp/tools")
async def mcp_tools() -> dict[str, Any]:
    try:
        return {"tools": await GitMcpClient(settings.target_repo_path).list_tools()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/rag/status")
def rag_status(db: Db) -> dict[str, Any]:
    state = RagService(db).get_index_state()
    if state is None:
        return {"status": "not_indexed", "indexed_files": 0, "total_chunks": 0}
    return {
        "status": state.status,
        "indexed_files": state.document_count,
        "total_chunks": state.chunk_count,
        "last_indexed_at": state.last_indexed_at,
        "commit_sha": state.commit_sha,
        "branch": state.branch,
        "message": state.last_error,
    }


@app.post("/api/rag/reindex", status_code=202)
def reindex(background: BackgroundTasks, db: Db) -> dict[str, Any]:
    if _index_lock.locked():
        return {"status": "indexing", "message": "Индексация уже выполняется"}
    if not Path(settings.target_repo_path).is_dir():
        raise HTTPException(status_code=503, detail="TARGET_REPO_PATH недоступен")
    RagService(db).update_index_state(status="indexing", repository_path=settings.target_repo_path)
    background.add_task(_run_index)
    return {"status": "indexing", "message": "Переиндексация запущена"}
