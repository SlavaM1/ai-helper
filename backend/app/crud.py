from collections.abc import Sequence
from typing import Any

from sqlalchemy import Integer, delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .models import (
    Conversation,
    IndexState,
    Message,
    MessageRole,
    RagChunk,
    RagDocument,
    utc_now,
)
from .schemas import (
    ConversationCreate,
    ConversationSettingsUpdate,
    ConversationUpdate,
    ConversationUsage,
    MessageCreate,
    RagChunkCreate,
    RagDocumentCreate,
)


class ConversationNotFoundError(LookupError):
    pass


def _commit(db: Session) -> None:
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise


class ConversationService:
    def __init__(self, db: Session):
        self.db = db

    def create(self, data: ConversationCreate) -> Conversation:
        values = data.model_dump(exclude_none=True)
        settings = get_settings()
        values.setdefault("model", settings.default_model)
        values.setdefault("context_window", settings.default_context_window)
        conversation = Conversation(**values)
        self.db.add(conversation)
        _commit(self.db)
        self.db.refresh(conversation)
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self.db.get(Conversation, conversation_id)

    def require(self, conversation_id: str) -> Conversation:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    def list(self, *, offset: int = 0, limit: int = 100) -> Sequence[Conversation]:
        statement = (
            select(Conversation)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .offset(offset)
            .limit(limit)
        )
        return self.db.scalars(statement).all()

    def update(self, conversation_id: str, data: ConversationUpdate) -> Conversation:
        conversation = self.require(conversation_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(conversation, name, value)
        conversation.updated_at = utc_now()
        _commit(self.db)
        self.db.refresh(conversation)
        return conversation

    def update_settings(
        self, conversation_id: str, data: ConversationSettingsUpdate
    ) -> Conversation:
        conversation = self.require(conversation_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(conversation, name, value)
        conversation.updated_at = utc_now()
        _commit(self.db)
        self.db.refresh(conversation)
        return conversation

    def delete(self, conversation_id: str) -> bool:
        conversation = self.get(conversation_id)
        if conversation is None:
            return False
        self.db.delete(conversation)
        _commit(self.db)
        return True

    def add_message(self, conversation_id: str, data: MessageCreate) -> Message:
        conversation = self.require(conversation_id)
        message = Message(conversation_id=conversation_id, **data.model_dump())
        self.db.add(message)
        if data.role == MessageRole.USER:
            conversation.user_message_count += 1
        conversation.updated_at = utc_now()
        _commit(self.db)
        self.db.refresh(message)
        return message

    def list_messages(
        self, conversation_id: str, *, offset: int = 0, limit: int = 1_000
    ) -> Sequence[Message]:
        self.require(conversation_id)
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
            .offset(offset)
            .limit(limit)
        )
        return self.db.scalars(statement).all()

    def count_messages(self, conversation_id: str, role: MessageRole | None = None) -> int:
        self.require(conversation_id)
        statement = select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
        if role is not None:
            statement = statement.where(Message.role == role)
        return int(self.db.scalar(statement) or 0)

    def usage(self, conversation_id: str) -> ConversationUsage:
        conversation = self.require(conversation_id)
        statement = select(
            func.count(Message.id),
            func.coalesce(func.sum(Message.input_tokens), 0),
            func.coalesce(func.sum(Message.output_tokens), 0),
            func.coalesce(func.sum(Message.total_tokens), 0),
            func.coalesce(func.sum(Message.summary_tokens), 0),
            func.coalesce(func.sum(func.cast(Message.tokens_estimated, Integer)), 0),
            func.coalesce(func.sum(Message.rag_chunks), 0),
            func.coalesce(func.sum(Message.total_ms), 0),
            func.coalesce(func.sum(Message.llm_ms), 0),
        ).where(Message.conversation_id == conversation_id)
        row = self.db.execute(statement).one()
        return ConversationUsage(
            conversation_id=conversation_id,
            message_count=row[0],
            user_message_count=conversation.user_message_count,
            input_tokens=row[1],
            output_tokens=row[2],
            total_tokens=row[3],
            summary_tokens=row[4],
            estimated_message_count=row[5],
            rag_chunks=row[6],
            total_ms=row[7],
            llm_ms=row[8],
        )
class RagService:
    def __init__(self, db: Session):
        self.db = db

    def create_document(self, data: RagDocumentCreate) -> RagDocument:
        values = data.model_dump()
        values["metadata_json"] = values.pop("metadata")
        document = RagDocument(**values)
        self.db.add(document)
        _commit(self.db)
        self.db.refresh(document)
        return document

    def get_document(self, document_id: str) -> RagDocument | None:
        return self.db.get(RagDocument, document_id)

    def get_document_by_path(self, source_path: str) -> RagDocument | None:
        return self.db.scalar(select(RagDocument).where(RagDocument.source_path == source_path))

    def list_documents(self, *, offset: int = 0, limit: int = 100) -> Sequence[RagDocument]:
        return self.db.scalars(
            select(RagDocument).order_by(RagDocument.source_path).offset(offset).limit(limit)
        ).all()

    def delete_document(self, document_id: str) -> bool:
        result = self.db.execute(delete(RagDocument).where(RagDocument.id == document_id))
        _commit(self.db)
        return bool(result.rowcount)

    def add_chunk(self, document_id: str, data: RagChunkCreate) -> RagChunk:
        if self.get_document(document_id) is None:
            raise LookupError(document_id)
        values = data.model_dump()
        values["metadata_json"] = values.pop("metadata")
        chunk = RagChunk(document_id=document_id, **values)
        self.db.add(chunk)
        _commit(self.db)
        self.db.refresh(chunk)
        return chunk

    def list_chunks(self, document_id: str) -> Sequence[RagChunk]:
        return self.db.scalars(
            select(RagChunk)
            .where(RagChunk.document_id == document_id)
            .order_by(RagChunk.chunk_index)
        ).all()

    def get_index_state(self, name: str = "default") -> IndexState | None:
        return self.db.scalar(select(IndexState).where(IndexState.name == name))

    def update_index_state(self, name: str = "default", **values: Any) -> IndexState:
        allowed = {
            "status",
            "repository_path",
            "revision",
            "branch",
            "commit_sha",
            "document_count",
            "chunk_count",
            "started_at",
            "completed_at",
            "last_indexed_at",
            "last_error",
            "metadata_json",
        }
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(f"Unknown index state fields: {', '.join(sorted(unknown))}")
        state = self.get_index_state(name)
        if state is None:
            state = IndexState(name=name)
            self.db.add(state)
        for field, value in values.items():
            setattr(state, field, value)
        state.updated_at = utc_now()
        _commit(self.db)
        self.db.refresh(state)
        return state


# Function aliases keep route handlers terse while the service remains easy to inject in tests.
def create_conversation(db: Session, data: ConversationCreate) -> Conversation:
    return ConversationService(db).create(data)


def get_conversation(db: Session, conversation_id: str) -> Conversation | None:
    return ConversationService(db).get(conversation_id)


def update_conversation(
    db: Session, conversation_id: str, data: ConversationUpdate
) -> Conversation:
    return ConversationService(db).update(conversation_id, data)


def delete_conversation(db: Session, conversation_id: str) -> bool:
    return ConversationService(db).delete(conversation_id)
