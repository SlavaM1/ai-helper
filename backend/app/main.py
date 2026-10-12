from __future__ import annotations

import asyncio
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
from .config import get_settings
from .crud import ConversationNotFoundError, ConversationService, RagService
from .database import SessionLocal, get_db, init_db
from .mcp_tools import GitMcpClient
from .rag import HybridRetriever, RagIndexer
from .schemas import ConversationCreate, ConversationRead, ConversationUpdate

logger = logging.getLogger(__name__)
settings = get_settings()
llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url)
_index_lock = threading.Lock()


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
    repo = Path(settings.target_repo_path)
    if settings.auto_index and repo.is_dir():
        asyncio.create_task(asyncio.to_thread(_run_index))
    yield


app = FastAPI(
    title="Developer AI Assistant API",
    version="1.0.0",
    description="DeepSeek agent with repository RAG and read-only Git MCP tools.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Db = Annotated[Session, Depends(get_db)]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def _service(db: Session) -> ConversationService:
    return ConversationService(db)


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Диалог не найден: {exc}")


def _message_payload(message: Any) -> dict[str, Any]:
    return AgentOrchestrator._message_payload(message)


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
    orchestrator = AgentOrchestrator(
        db,
        settings,
        llm,
        HybridRetriever(db),
        GitMcpClient(repo),
    )

    async def events():
        try:
            async for item in orchestrator.chat(conversation_id, data.message):
                yield item
        except Exception as exc:
            logger.exception("Agent request failed")
            import json

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
