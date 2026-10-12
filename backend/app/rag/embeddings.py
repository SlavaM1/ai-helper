"""Compact local multilingual ONNX embeddings with test injection points."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from typing import Any


def normalize_embedding(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        return values
    return [value / norm for value in values]


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        *,
        model: Any | None = None,
        encoder: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
    ) -> None:
        self.model_name = model_name
        self._model = model
        self._encoder = encoder

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - depends on deployment image
                raise RuntimeError("fastembed is required for RAG embeddings") from exc
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._encoder is not None:
            encoded = self._encoder(texts)
        else:
            encoded = list(self._load_model().embed(list(texts)))
        return [normalize_embedding(vector) for vector in encoded]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text])[0]

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Compatibility alias for callers with a generic embedding interface."""

        return self.embed_documents(texts)
