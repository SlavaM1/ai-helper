from __future__ import annotations

import math
import subprocess
from pathlib import Path

from app.models import IndexState, RagChunk, RagDocument
from app.rag import HybridRetriever, RagIndexer, RepoScanner, StructuralChunker


class FakeEmbedder:
    words = ("payment", "authentication", "database", "python")

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        values = [float(lowered.count(word)) for word in self.words]
        values.append(0.1)
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "https://github.com/example/payments.git")
    (repo / "README.md").write_text(
        "# Payment service\n\nThe payment gateway retries failed charges.\n\n"
        "## Authentication\n\nAPI_KEY=super-secret-value\nAuthentication uses OAuth.\n"
    )
    (repo / "worker.py").write_text("def process_payment():\n    return 'database payment'\n")
    (repo / "notes.txt").write_text("Operational notes.\n")
    (repo / "ignored.exe").write_bytes(b"not text")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "leak.js").write_text("password=hunter2")
    (repo / "secrets.yaml").write_text("TOKEN=hidden")
    _git(repo, "add", "README.md", "worker.py", "notes.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_scanner_exclusions_redaction_and_line_chunks(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    files = RepoScanner(repo, max_file_size=100_000, max_files=10).scan()

    assert {item.path for item in files} == {"README.md", "notes.txt", "worker.py"}
    readme = next(item for item in files if item.path == "README.md")
    assert "super-secret-value" not in readme.content
    assert "[REDACTED]" in readme.content

    chunks = StructuralChunker(max_chars=80, overlap_chars=15).chunk(readme)
    assert chunks[0].title == "Payment service"
    assert chunks[0].line_start == 1
    assert chunks[0].line_end >= chunks[0].line_start
    assert any(chunk.section == "Authentication" for chunk in chunks)
    assert all(chunk.id and chunk.content_hash for chunk in chunks)


def test_incremental_index_retrieval_metadata_and_delete(db, tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    embedder = FakeEmbedder()
    indexer = RagIndexer(db, repo, embedder=embedder)

    first = indexer.index()
    assert first.added == 3
    assert db.query(RagDocument).count() == 3
    assert db.query(RagChunk).count() >= 3
    chunk = db.query(RagChunk).filter(RagChunk.path == "README.md").first()
    assert chunk is not None
    assert chunk.metadata_json["path"] == "README.md"
    assert chunk.metadata_json["repo"] == "example/payments"
    assert chunk.metadata_json["branch"]
    assert chunk.metadata_json["commit"]
    assert chunk.metadata_json["chunk_id"]
    assert chunk.start_line == 1

    results = HybridRetriever(db, embedder=embedder).search("payment gateway", top_k=2)
    assert results
    assert "payment" in results[0].content.lower()
    assert results[0].metadata["permalink"].startswith(
        "https://github.com/example/payments/blob/"
    )
    assert "#L" in results[0].metadata["permalink"]

    (repo / "worker.py").unlink()
    (repo / "README.md").write_text("# Database\n\nDatabase migration instructions.\n")
    second = indexer.reindex()
    assert second.updated == 1
    assert second.deleted == 1
    assert second.unchanged == 1
    assert db.query(RagDocument).filter(RagDocument.source_path == "worker.py").first() is None
    assert not db.query(RagChunk).filter(RagChunk.path == "worker.py").all()
    state = db.query(IndexState).one()
    assert state.document_count == 2
    assert state.chunk_count == db.query(RagChunk).count()
