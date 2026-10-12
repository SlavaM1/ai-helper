"""Repository indexing and hybrid retrieval."""

from .chunker import Chunk, StructuralChunker
from .embeddings import SentenceTransformerEmbedder
from .indexer import IndexStats, RagIndexer
from .retrieval import HybridRetriever, RetrievalResult, github_permalink
from .scanner import RepoFile, RepoScanner, redact_secrets

__all__ = [
    "Chunk",
    "HybridRetriever",
    "IndexStats",
    "RagIndexer",
    "RepoFile",
    "RepoScanner",
    "RetrievalResult",
    "SentenceTransformerEmbedder",
    "StructuralChunker",
    "github_permalink",
    "redact_secrets",
]
