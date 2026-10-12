from sqlalchemy import func, select, text

from app.crud import ConversationService, RagService
from app.database import create_database_engine
from app.models import Message, MessageRole, RagChunk, RagDocument
from app.schemas import (
    ConversationCreate,
    ConversationSettingsUpdate,
    ConversationUpdate,
    MessageCreate,
    RagChunkCreate,
    RagDocumentCreate,
)


def test_create_update_and_settings(db):
    service = ConversationService(db)
    conversation = service.create(ConversationCreate(title="API design"))

    assert conversation.id
    assert conversation.model == "deepseek-flash"
    assert conversation.context_window == 16_384

    updated = service.update(conversation.id, ConversationUpdate(title="REST API design"))
    configured = service.update_settings(
        conversation.id,
        ConversationSettingsUpdate(model="deepseek-reasoner", context_window=32_768),
    )

    assert updated.title == "REST API design"
    assert configured.model == "deepseek-reasoner"
    assert configured.context_window == 32_768


def test_message_count_and_usage(db):
    service = ConversationService(db)
    conversation = service.create(ConversationCreate())
    service.add_message(
        conversation.id,
        MessageCreate(
            role=MessageRole.USER,
            content="Explain this repository",
            input_tokens=10,
            output_tokens=0,
            total_ms=20,
            tokens_estimated=True,
        ),
    )
    service.add_message(
        conversation.id,
        MessageCreate(
            role=MessageRole.ASSISTANT,
            content="Response",
            input_tokens=10,
            output_tokens=15,
            summary_tokens=3,
            rag_chunks=2,
            total_ms=120,
            llm_ms=100,
        ),
    )

    usage = service.usage(conversation.id)

    assert service.count_messages(conversation.id) == 2
    assert service.count_messages(conversation.id, MessageRole.USER) == 1
    assert usage.message_count == 2
    assert usage.user_message_count == 1
    assert usage.input_tokens == 20
    assert usage.output_tokens == 15
    assert usage.total_tokens == 35
    assert usage.summary_tokens == 3
    assert usage.estimated_message_count == 1
    assert usage.rag_chunks == 2
    assert usage.total_ms == 140
    assert usage.llm_ms == 100


def test_delete_cascades_messages_but_not_global_rag(db):
    conversations = ConversationService(db)
    rag = RagService(db)
    conversation = conversations.create(ConversationCreate())
    conversations.add_message(
        conversation.id, MessageCreate(role=MessageRole.USER, content="Question")
    )
    document = rag.create_document(
        RagDocumentCreate(source_path="src/example.py", content_hash="abc", size_bytes=20)
    )
    rag.add_chunk(
        document.id,
        RagChunkCreate(chunk_index=0, content="def example(): pass", content_hash="def"),
    )

    assert conversations.delete(conversation.id) is True
    assert conversations.delete(conversation.id) is False
    assert db.scalar(select(func.count(Message.id))) == 0
    assert db.scalar(select(func.count(RagDocument.id))) == 1
    assert db.scalar(select(func.count(RagChunk.id))) == 1


def test_rag_document_delete_cascades_chunks(db):
    rag = RagService(db)
    document = rag.create_document(
        RagDocumentCreate(
            source_path="README.md",
            source_type="file",
            content_hash="hash",
            size_bytes=100,
            mime_type="text/markdown",
            language="markdown",
            metadata={"revision": "abc123"},
        )
    )
    rag.add_chunk(
        document.id,
        RagChunkCreate(
            chunk_index=0,
            content="# Readme",
            content_hash="chunk-hash",
            token_count=3,
            start_line=1,
            end_line=1,
        ),
    )

    assert rag.delete_document(document.id) is True
    assert db.scalar(select(func.count(RagChunk.id))) == 0


def test_index_state_is_created_and_updated(db):
    rag = RagService(db)
    state = rag.update_index_state(status="running", repository_path="/repos/project")
    same_state = rag.update_index_state(
        status="ready", revision="abc123", document_count=2, chunk_count=5
    )

    assert same_state.id == state.id
    assert same_state.status == "ready"
    assert same_state.document_count == 2
    assert same_state.chunk_count == 5


def test_sqlite_connection_settings(tmp_path):
    engine = create_database_engine(
        f"sqlite:///{tmp_path / 'settings.db'}", busy_timeout_ms=7_500
    )
    with engine.connect() as connection:
        foreign_keys = connection.scalar(text("PRAGMA foreign_keys"))
        busy_timeout = connection.scalar(text("PRAGMA busy_timeout"))
        journal_mode = connection.scalar(text("PRAGMA journal_mode"))
    engine.dispose()

    assert foreign_keys == 1
    assert busy_timeout == 7_500
    assert journal_mode == "wal"
