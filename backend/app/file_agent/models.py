"""Additive, durable task history; no provider reasoning is stored."""

from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import new_uuid, utc_now


class FileAgentTask(Base):
    __tablename__ = "file_agent_tasks"
    __table_args__ = (CheckConstraint(
        "status IN ('queued','running','completed','failed','applied','discarded')",
        name="ck_file_agent_task_status",
    ),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    base_sha: Mapped[str] = mapped_column(String(64))
    workspace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    operations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    read_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
