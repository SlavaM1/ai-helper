"""Hybrid semantic and lexical retrieval over persisted RAG chunks."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from .embeddings import SentenceTransformerEmbedder, normalize_embedding
from .indexer import _first_column


def _github_repo(value: str) -> str | None:
    value = value.strip().removesuffix(".git").rstrip("/")
    if value.startswith("git@github.com:"):
        value = value[len("git@github.com:") :]
    elif "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            return None
        value = parsed.path.strip("/")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return value
    return None


def github_permalink(metadata: Mapping[str, Any]) -> str | None:
    repo = _github_repo(str(metadata.get("repo", "")))
    path = str(metadata.get("path", "")).strip("/")
    revision = str(metadata.get("commit") or metadata.get("branch") or "")
    if not repo or not path or not revision:
        return None
    url = f"https://github.com/{repo}/blob/{quote(revision, safe='')}/{quote(path, safe='/')}"
    start = metadata.get("line_start") or metadata.get("start_line")
    end = metadata.get("line_end") or metadata.get("end_line")
    if isinstance(start, int) or str(start).isdigit():
        url += f"#L{int(start)}"
        if (isinstance(end, int) or str(end).isdigit()) and int(end) != int(start):
            url += f"-L{int(end)}"
    return url


@dataclass(frozen=True)
class RetrievalResult(Mapping[str, Any]):
    content: str
    score: float
    semantic_score: float
    keyword_score: float
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "semantic_score": self.semantic_score,
            "keyword_score": self.keyword_score,
            "metadata": self.metadata,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return 5


class HybridRetriever:
    def __init__(
        self,
        session: Any,
        *,
        embedder: Any | None = None,
        chunk_model: type[Any] | None = None,
        document_model: type[Any] | None = None,
        semantic_weight: float = 0.7,
    ) -> None:
        if not 0 <= semantic_weight <= 1:
            raise ValueError("semantic_weight must be between zero and one")
        self.session_source = session
        self.embedder = embedder or SentenceTransformerEmbedder()
        if chunk_model is None:
            from app.models import RagChunk, RagDocument

            chunk_model = RagChunk
            document_model = document_model or RagDocument
        self.chunk_model = chunk_model
        self.document_model = document_model
        self.semantic_weight = semantic_weight

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

    def search(
        self,
        query: str,
        top_k: int = 8,
        threshold: float = 0.0,
        token_budget: int = 4_000,
    ) -> list[RetrievalResult]:
        query = query.strip()
        if not query or top_k <= 0 or token_budget <= 0:
            return []
        query_vector = self._embed_query(query)
        with self._session() as session:
            chunks = session.query(self.chunk_model).all()
            keyword_scores = self._keyword_scores(session, chunks, query)
            documents = self._documents(session)

        ranked: list[RetrievalResult] = []
        content_field = _first_column(self.chunk_model, "content", "text")
        embedding_field = _first_column(self.chunk_model, "embedding", "embedding_json", "embedding_blob", "vector")
        id_field = _first_column(self.chunk_model, "id", "chunk_id")
        if not content_field or not id_field:
            return []
        for chunk in chunks:
            content = str(getattr(chunk, content_field))
            key = str(getattr(chunk, id_field))
            vector = self._decode_vector(getattr(chunk, embedding_field, None)) if embedding_field else []
            semantic = self._cosine(query_vector, vector) if vector else 0.0
            keyword = keyword_scores.get(key, 0.0)
            metadata = self._metadata(chunk, documents)
            path_boost = self._path_boost(query, str(metadata.get("path", "")))
            score = min(
                1.0,
                self.semantic_weight * max(semantic, 0.0)
                + (1 - self.semantic_weight) * keyword
                + path_boost,
            )
            if score < threshold:
                continue
            metadata["permalink"] = github_permalink(metadata)
            ranked.append(
                RetrievalResult(
                    content=content,
                    score=score,
                    semantic_score=semantic,
                    keyword_score=keyword,
                    metadata=metadata,
                )
            )

        ranked.sort(key=lambda item: (item.score, item.semantic_score), reverse=True)
        selected: list[RetrievalResult] = []
        used_tokens = 0
        seen: set[str] = set()
        path_counts: dict[str, int] = {}
        path_ranges: dict[str, list[tuple[int, int]]] = {}
        for result in ranked:
            identity = re.sub(r"\s+", " ", result.content).strip()
            if identity in seen:
                continue
            path = str(result.metadata.get("path", ""))
            start = int(result.metadata.get("line_start") or 0)
            end = int(result.metadata.get("line_end") or start)
            if path and path_counts.get(path, 0) >= 2:
                continue
            if path and start and any(start <= existing_end and end >= existing_start for existing_start, existing_end in path_ranges.get(path, [])):
                continue
            tokens = max(1, len(result.content) // 4)
            if used_tokens + tokens > token_budget:
                continue
            selected.append(result)
            seen.add(identity)
            if path:
                path_counts[path] = path_counts.get(path, 0) + 1
                path_ranges.setdefault(path, []).append((start, end))
            used_tokens += tokens
            if len(selected) >= top_k:
                break
        return selected

    retrieve = search

    @staticmethod
    def _path_boost(query: str, path: str) -> float:
        query_lower = query.lower()
        path_lower = path.lower()
        boost = 0.0
        exact_hints = {
            "pom.xml": 0.45,
            "build.gradle": 0.45,
            "package.json": 0.4,
            "application.properties": 0.35,
        }
        for hint, weight in exact_hints.items():
            if hint in query_lower and path_lower.endswith(hint):
                boost = max(boost, weight)
        if "application entry point" in query_lower and path_lower.endswith("application.java"):
            boost = max(boost, 0.4)
        layer_hints = ("controller", "service", "repositor", "entities", "models", "migration")
        if any(hint in query_lower and hint in path_lower for hint in layer_hints):
            boost = max(boost, 0.18)
        return boost

    def _keyword_scores(self, session: Any, chunks: list[Any], query: str) -> dict[str, float]:
        terms = list(dict.fromkeys(re.findall(r"[\w.-]{2,}", query.lower(), re.UNICODE)))
        if terms:
            try:
                from sqlalchemy import text

                expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
                rows = session.execute(
                    text(
                        "SELECT chunk_key, bm25(rag_chunks_fts) AS rank "
                        "FROM rag_chunks_fts WHERE rag_chunks_fts MATCH :query ORDER BY rank LIMIT 200"
                    ),
                    {"query": expression},
                ).all()
                if rows:
                    count = len(rows)
                    return {str(row[0]): 1.0 - (index / max(count, 1)) for index, row in enumerate(rows)}
            except Exception:
                pass

        id_field = _first_column(self.chunk_model, "id", "chunk_id")
        content_field = _first_column(self.chunk_model, "content", "text")
        if not id_field or not content_field or not terms:
            return {}
        scores: dict[str, float] = {}
        query_terms = set(terms)
        for chunk in chunks:
            content_terms = set(re.findall(r"[\w.-]{2,}", str(getattr(chunk, content_field)).lower(), re.UNICODE))
            overlap = len(query_terms & content_terms)
            if overlap:
                scores[str(getattr(chunk, id_field))] = overlap / len(query_terms)
        return scores

    def _documents(self, session: Any) -> dict[Any, Any]:
        if self.document_model is None:
            return {}
        primary_key = _first_column(self.document_model, "id")
        if not primary_key:
            return {}
        return {
            getattr(document, primary_key): document
            for document in session.query(self.document_model).all()
        }

    def _metadata(self, chunk: Any, documents: dict[Any, Any]) -> dict[str, Any]:
        metadata_field = _first_column(self.chunk_model, "metadata_json", "source_metadata", "attributes")
        metadata = self._decode_metadata(getattr(chunk, metadata_field, None)) if metadata_field else {}
        document_fk = _first_column(self.chunk_model, "document_id", "rag_document_id")
        document = documents.get(getattr(chunk, document_fk, None)) if document_fk else None
        mappings = {
            "path": ("path", "file_path", "source_path"),
            "title": ("title",),
            "section": ("section",),
            "line_start": ("line_start", "start_line"),
            "line_end": ("line_end", "end_line"),
            "hash": ("content_hash", "hash", "sha256"),
            "repo": ("repo", "repository"),
            "branch": ("branch",),
            "commit": ("commit", "commit_sha", "last_commit"),
            "chunk_id": ("chunk_id", "id"),
        }
        for target, candidates in mappings.items():
            if metadata.get(target) is not None:
                continue
            for source in (chunk, document):
                if source is None:
                    continue
                for candidate in candidates:
                    value = getattr(source, candidate, None)
                    if value is not None:
                        metadata[target] = value
                        break
                if target in metadata:
                    break
        return metadata

    def _embed_query(self, query: str) -> list[float]:
        if hasattr(self.embedder, "embed_query"):
            return normalize_embedding(self.embedder.embed_query(query))
        encoded = self.embedder.encode([query])
        return normalize_embedding(encoded[0])

    @staticmethod
    def _decode_vector(value: Any) -> list[float]:
        if value is None:
            return []
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
        return normalize_embedding(value)

    @staticmethod
    def _decode_metadata(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value)

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True)) / (
            math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)) or 1.0
        )
