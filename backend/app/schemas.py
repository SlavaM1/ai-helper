from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import MessageRole


class ApiSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConversationCreate(ApiSchema):
    title: str = Field(default="New conversation", min_length=1, max_length=255)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    context_window: int | None = Field(default=None, ge=4_096, le=131_072)


class ConversationUpdate(ApiSchema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    context_window: int | None = Field(default=None, ge=4_096, le=131_072)
    summary: str | None = None
    summary_updated_at: datetime | None = None
    summarized_through_message_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> "ConversationUpdate":
        for field in ("title", "model", "context_window"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ConversationSettingsUpdate(ApiSchema):
    model: str | None = Field(default=None, min_length=1, max_length=255)
    context_window: int | None = Field(default=None, ge=4_096, le=131_072)

    @model_validator(mode="after")
    def reject_null_settings(self) -> "ConversationSettingsUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ConversationRead(ApiSchema):
    id: str
    title: str
    model: str
    context_window: int
    summary: str | None
    summary_updated_at: datetime | None
    summarized_through_message_id: str | None
    user_message_count: int
    created_at: datetime
    updated_at: datetime


class MessageCreate(ApiSchema):
    role: MessageRole
    content: str = Field(min_length=1)
    model: str | None = Field(default=None, max_length=255)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    tokens_estimated: bool = False
    summary_tokens: int = Field(default=0, ge=0)
    total_ms: int | None = Field(default=None, ge=0)
    llm_ms: int | None = Field(default=None, ge=0)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    rag_chunks: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def fill_total_tokens(self) -> "MessageCreate":
        if self.total_tokens is None and self.input_tokens is not None and self.output_tokens is not None:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class MessageRead(MessageCreate):
    id: str
    conversation_id: str
    created_at: datetime


class ConversationUsage(ApiSchema):
    conversation_id: str
    message_count: int
    user_message_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    summary_tokens: int
    estimated_message_count: int
    rag_chunks: int
    total_ms: int
    llm_ms: int


class RagChunkCreate(ApiSchema):
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1)
    content_hash: str = Field(min_length=1, max_length=128)
    token_count: int = Field(default=0, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_lines(self) -> "RagChunkCreate":
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class RagChunkRead(RagChunkCreate):
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    id: str
    document_id: str
    created_at: datetime


class RagDocumentCreate(ApiSchema):
    source_path: str = Field(min_length=1)
    source_type: str = Field(default="file", min_length=1, max_length=64)
    title: str | None = None
    content_hash: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    mime_type: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=64)
    repository: str | None = None
    branch: str | None = Field(default=None, max_length=255)
    commit_sha: str | None = Field(default=None, max_length=255)
    modified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagDocumentRead(RagDocumentCreate):
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    id: str
    indexed_at: datetime


class IndexStateRead(ApiSchema):
    id: str
    name: str
    status: str
    repository_path: str | None
    revision: str | None
    branch: str | None
    commit_sha: str | None
    document_count: int
    chunk_count: int
    started_at: datetime | None
    completed_at: datetime | None
    last_indexed_at: datetime | None
    last_error: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    updated_at: datetime
