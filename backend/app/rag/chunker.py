"""Line-aware structural chunks with deterministic identifiers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .scanner import RepoFile

_SECTION_PATTERNS = (
    re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$"),
    re.compile(r"^\s*(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:async\s+)?(?:def|function)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[\w<>,.?\[\]]+\s+([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|:)?\s*$"),
)


@dataclass(frozen=True)
class Chunk:
    id: str
    content: str
    path: str
    title: str
    section: str | None
    line_start: int
    line_end: int
    content_hash: str
    index: int

    def metadata(self, *, repo: str, branch: str, commit: str) -> dict[str, object]:
        return {
            "path": self.path,
            "title": self.title,
            "section": self.section,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "hash": self.content_hash,
            "repo": repo,
            "branch": branch,
            "commit": commit,
            "chunk_id": self.id,
        }


class StructuralChunker:
    def __init__(self, *, max_chars: int = 1_200, overlap_chars: int = 200) -> None:
        if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("expected 0 <= overlap_chars < max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def chunk(self, source: RepoFile) -> list[Chunk]:
        lines = source.content.splitlines(keepends=True)
        if not lines:
            return []

        units: list[tuple[int, str, str | None, bool]] = []
        active_section: str | None = None
        for line_number, line in enumerate(lines, start=1):
            new_section = self._section(line)
            active_section = new_section or active_section
            for offset in range(0, max(1, len(line)), self.max_chars):
                units.append(
                    (line_number, line[offset : offset + self.max_chars], active_section, bool(new_section and offset == 0))
                )

        chunks: list[Chunk] = []
        start = 0
        while start < len(units):
            size = 0
            end = start
            structural_boundary = False
            while end < len(units):
                if end > start and units[end][3]:
                    structural_boundary = True
                    break
                candidate_size = size + len(units[end][1])
                if end > start and candidate_size > self.max_chars:
                    break
                size = candidate_size
                end += 1
                if size >= self.max_chars:
                    break

            text = "".join(unit[1] for unit in units[start:end]).rstrip()
            if text:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                line_start = units[start][0]
                line_end = units[end - 1][0]
                chunk_id = hashlib.sha256(
                    f"{source.path}\0{line_start}\0{digest}".encode()
                ).hexdigest()
                chunks.append(
                    Chunk(
                        id=chunk_id,
                        content=text,
                        path=source.path,
                        title=source.title,
                        section=units[start][2],
                        line_start=line_start,
                        line_end=line_end,
                        content_hash=digest,
                        index=len(chunks),
                    )
                )
            if end >= len(units):
                break

            if structural_boundary:
                start = end
                continue

            overlap = 0
            next_start = end
            while next_start > start + 1 and overlap < self.overlap_chars:
                next_start -= 1
                overlap += len(units[next_start][1])
            start = next_start
        return chunks

    @staticmethod
    def _section(line: str) -> str | None:
        for pattern in _SECTION_PATTERNS:
            match = pattern.match(line)
            if match:
                return match.group(1).strip()
        return None
