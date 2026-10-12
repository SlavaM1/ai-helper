"""Incremental persistence for repository documents and chunks."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chunker import StructuralChunker
from .embeddings import SentenceTransformerEmbedder, normalize_embedding
from .scanner import RepoScanner


@dataclass(frozen=True)
class IndexStats:
    scanned: int
    added: int
    updated: int
    deleted: int
    unchanged: int
    chunks: int
    branch: str
    commit: str


def _model_columns(model: type[Any]) -> dict[str, Any]:
    table = getattr(model, "__table__", None)
    if table is None:
        return {}
    return {column.key: column for column in table.columns}


def _first_column(model: type[Any], *names: str) -> str | None:
    columns = _model_columns(model)
    return next((name for name in names if name in columns), None)


def _json_value(model: type[Any], field: str, value: Any) -> Any:
    column = _model_columns(model).get(field)
    if column is not None:
        if column.type.__class__.__name__.upper() == "JSON":
            return value
        try:
            python_type = column.type.python_type
        except (AttributeError, NotImplementedError):
            python_type = None
        if python_type in (dict, list):
            return value
        if python_type is bytes:
            return json.dumps(value, separators=(",", ":")).encode("utf-8")
    return json.dumps(value, separators=(",", ":"))


def _filtered_kwargs(model: type[Any], values: dict[str, Any]) -> dict[str, Any]:
    columns = _model_columns(model)
    return {key: value for key, value in values.items() if key in columns and value is not None}


class RagIndexer:
    def __init__(
        self,
        session: Any,
        repo_path: str | Path,
        *,
        embedder: Any | None = None,
        scanner: RepoScanner | None = None,
        chunker: StructuralChunker | None = None,
        document_model: type[Any] | None = None,
        chunk_model: type[Any] | None = None,
        state_model: type[Any] | None = None,
    ) -> None:
        self.session_source = session
        self.repo_path = Path(repo_path).expanduser().resolve(strict=True)
        self.embedder = embedder or SentenceTransformerEmbedder()
        self.scanner = scanner or RepoScanner(self.repo_path)
        self.chunker = chunker or StructuralChunker()
        if document_model is None or chunk_model is None:
            from app.models import IndexState, RagChunk, RagDocument

            document_model = document_model or RagDocument
            chunk_model = chunk_model or RagChunk
            state_model = state_model or IndexState
        self.document_model = document_model
        self.chunk_model = chunk_model
        self.state_model = state_model

    @contextmanager
    def _session(self) -> Iterator[Any]:
        if hasattr(self.session_source, "query"):
            yield self.session_source
            return
        session = self.session_source()
        try:
            yield session
        finally:
            session.close()

    def index(self, *, force: bool = False) -> IndexStats:
        files = self.scanner.scan()
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
        commit = self._git("rev-parse", "HEAD")
        repo = self._repository_name()
        with self._session() as session:
            try:
                stats = self._index(session, files, repo, branch, commit, force)
                session.commit()
                return stats
            except Exception:
                session.rollback()
                raise

    def reindex(self, *, force: bool = False) -> IndexStats:
        return self.index(force=force)

    def _index(
        self,
        session: Any,
        files: list[Any],
        repo: str,
        branch: str,
        commit: str,
        force: bool,
    ) -> IndexStats:
        path_field = _first_column(self.document_model, "path", "file_path", "source_path")
        hash_field = _first_column(self.document_model, "content_hash", "hash", "sha256")
        if not path_field or not hash_field:
            raise RuntimeError("RagDocument must expose path and content_hash columns")

        documents = session.query(self.document_model).all()
        existing = {getattr(document, path_field): document for document in documents}
        current_paths = {source.path for source in files}
        deleted = 0
        if getattr(self.scanner, "scan_complete", True):
            for path, document in existing.items():
                if path not in current_paths:
                    self._delete_document(session, document)
                    deleted += 1

        added = updated = unchanged = 0
        for source in files:
            document = existing.get(source.path)
            document_commit = getattr(document, "commit_sha", None) if document is not None else None
            document_branch = getattr(document, "branch", None) if document is not None else None
            if (
                document is not None
                and not force
                and getattr(document, hash_field) == source.content_hash
                and document_commit == commit
                and document_branch == branch
            ):
                unchanged += 1
                continue
            chunks = self.chunker.chunk(source)
            embeddings = self._embed_documents([chunk.content for chunk in chunks])
            if document is None:
                document = self._new_document(source, repo, branch, commit)
                session.add(document)
                session.flush()
                added += 1
            else:
                self._delete_chunks(session, document)
                self._update_document(document, source, repo, branch, commit)
                updated += 1
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                session.add(self._new_chunk(document, chunk, embedding, repo, branch, commit))

        session.flush()
        total_documents = session.query(self.document_model).count()
        total_chunks = session.query(self.chunk_model).count()
        self._update_state(session, total_documents, total_chunks, branch, commit)
        self._rebuild_fts(session)
        return IndexStats(
            scanned=len(files),
            added=added,
            updated=updated,
            deleted=deleted,
            unchanged=unchanged,
            chunks=total_chunks,
            branch=branch,
            commit=commit,
        )

    def _new_document(self, source: Any, repo: str, branch: str, commit: str) -> Any:
        now = datetime.now(UTC)
        values = {
            "path": source.path,
            "file_path": source.path,
            "source_path": source.path,
            "content_hash": source.content_hash,
            "hash": source.content_hash,
            "sha256": source.content_hash,
            "title": source.title,
            "size": source.size,
            "size_bytes": source.size,
            "repo": repo,
            "repository": repo,
            "branch": branch,
            "commit": commit,
            "commit_sha": commit,
            "indexed_at": now,
            "updated_at": now,
            "created_at": now,
        }
        return self.document_model(**_filtered_kwargs(self.document_model, values))

    def _update_document(self, document: Any, source: Any, repo: str, branch: str, commit: str) -> None:
        replacement = self._new_document(source, repo, branch, commit)
        for field in _model_columns(self.document_model):
            if field in {"id", "created_at"}:
                continue
            value = getattr(replacement, field, None)
            if value is not None:
                setattr(document, field, value)

    def _new_chunk(
        self, document: Any, chunk: Any, embedding: list[float], repo: str, branch: str, commit: str
    ) -> Any:
        columns = _model_columns(self.chunk_model)
        metadata = chunk.metadata(repo=repo, branch=branch, commit=commit)
        embedding_field = _first_column(self.chunk_model, "embedding", "embedding_json", "embedding_blob", "vector")
        metadata_field = _first_column(self.chunk_model, "metadata_json", "source_metadata", "attributes")
        document_pk = _first_column(self.document_model, "id")
        document_fk = _first_column(self.chunk_model, "document_id", "rag_document_id")
        values: dict[str, Any] = {
            "chunk_id": chunk.id,
            "content": chunk.content,
            "text": chunk.content,
            "chunk_index": chunk.index,
            "position": chunk.index,
            "path": chunk.path,
            "title": chunk.title,
            "section": chunk.section,
            "line_start": chunk.line_start,
            "start_line": chunk.line_start,
            "line_end": chunk.line_end,
            "end_line": chunk.line_end,
            "content_hash": chunk.content_hash,
            "hash": chunk.content_hash,
            "token_count": max(1, len(chunk.content) // 4),
        }
        if document_fk and document_pk:
            values[document_fk] = getattr(document, document_pk)
        if "document" in getattr(self.chunk_model, "__mapper__", ()).relationships:
            values["document"] = document
        if embedding_field:
            values[embedding_field] = _json_value(self.chunk_model, embedding_field, embedding)
        if metadata_field:
            values[metadata_field] = _json_value(self.chunk_model, metadata_field, metadata)

        id_column = columns.get("id")
        if id_column is not None and not getattr(id_column, "autoincrement", False):
            try:
                if id_column.type.python_type is str:
                    values["id"] = chunk.id
            except (AttributeError, NotImplementedError):
                pass
        return self.chunk_model(**_filtered_kwargs(self.chunk_model, values))

    def _delete_document(self, session: Any, document: Any) -> None:
        self._delete_chunks(session, document)
        session.delete(document)

    def _delete_chunks(self, session: Any, document: Any) -> None:
        foreign_key = _first_column(self.chunk_model, "document_id", "rag_document_id")
        primary_key = _first_column(self.document_model, "id")
        if foreign_key and primary_key:
            session.query(self.chunk_model).filter(
                getattr(self.chunk_model, foreign_key) == getattr(document, primary_key)
            ).delete(synchronize_session=False)

    def _update_state(
        self, session: Any, document_count: int, chunk_count: int, branch: str, commit: str
    ) -> None:
        if self.state_model is None:
            return
        state = session.query(self.state_model).first()
        now = datetime.now(UTC)
        values = {
            "status": "ready",
            "branch": branch,
            "last_commit": commit,
            "commit": commit,
            "commit_sha": commit,
            "revision": commit,
            "repository_path": str(self.repo_path),
            "document_count": document_count,
            "chunk_count": chunk_count,
            "completed_at": now,
            "last_indexed_at": now,
            "indexed_at": now,
            "updated_at": now,
            "error": None,
            "error_message": None,
        }
        if state is None:
            state = self.state_model(**_filtered_kwargs(self.state_model, values))
            session.add(state)
        else:
            for field, value in _filtered_kwargs(self.state_model, values).items():
                setattr(state, field, value)

    def _rebuild_fts(self, session: Any) -> None:
        try:
            from sqlalchemy import text

            session.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts "
                    "USING fts5(chunk_key UNINDEXED, content, tokenize='unicode61')"
                )
            )
            session.execute(text("DELETE FROM rag_chunks_fts"))
            id_field = _first_column(self.chunk_model, "id", "chunk_id")
            content_field = _first_column(self.chunk_model, "content", "text")
            if not id_field or not content_field:
                return
            rows = session.query(self.chunk_model).all()
            if rows:
                session.execute(
                    text("INSERT INTO rag_chunks_fts(chunk_key, content) VALUES (:key, :content)"),
                    [
                        {"key": str(getattr(row, id_field)), "content": getattr(row, content_field)}
                        for row in rows
                    ],
                )
        except Exception:
            # FTS5 is optional in SQLite builds; retrieval has a lexical fallback.
            return

    def _embed_documents(self, texts: list[str]) -> list[list[float]]:
        if hasattr(self.embedder, "embed_documents"):
            vectors = self.embedder.embed_documents(texts)
        else:
            vectors = self.embedder.encode(texts)
        return [normalize_embedding(vector) for vector in vectors]

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.repo_path}",
                "-C",
                str(self.repo_path),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _repository_name(self) -> str:
        remote = self._git("remote", "get-url", "origin")
        if remote:
            value = remote.removesuffix(".git").rstrip("/")
            if ":" in value and "://" not in value:
                value = value.split(":", 1)[1]
            elif "/" in value:
                value = "/".join(value.split("/")[-2:])
            return value
        return self.repo_path.name
