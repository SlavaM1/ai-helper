"""Safe, bounded scanner for a source repository."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".java", ".js", ".ts", ".tsx", ".py", ".xml", ".properties", ".html"}
)
EXCLUDED_PARTS = frozenset(
    {".git", "node_modules", "venv", ".venv", "build", "dist", "target", "static", "vendor", "uploads", "env", "secrets", "binaries"}
)

_SECRET_NAME = r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|client[_-]?secret|password|passwd|secret(?:_key)?|private[_-]?key)"
_ASSIGNMENT_SECRET = re.compile(
    rf"(?i)(\b{_SECRET_NAME}\b\s*[=:]\s*)([^\s,;#]+|[\"'][^\"']*[\"'])"
)
_JSON_SECRET = re.compile(
    rf'(?i)(["\']{_SECRET_NAME}["\']\s*:\s*)(["\'][^"\']*["\'])'
)
_TOKEN_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)


@dataclass(frozen=True)
class RepoFile:
    path: str
    content: str
    content_hash: str
    size: int
    title: str


def redact_secrets(text: str) -> str:
    """Redact common credential forms while preserving newline positions."""

    def private_key_replacement(match: re.Match[str]) -> str:
        return "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n")

    text = _PRIVATE_KEY.sub(private_key_replacement, text)
    text = _JSON_SECRET.sub(lambda match: match.group(1) + '"[REDACTED]"', text)
    text = _ASSIGNMENT_SECRET.sub(lambda match: match.group(1) + "[REDACTED]", text)
    return _TOKEN_SECRET.sub("[REDACTED]", text)


class RepoScanner:
    def __init__(
        self,
        repo_path: str | os.PathLike[str],
        *,
        max_file_size: int = 1_000_000,
        max_files: int = 10_000,
        max_total_size: int = 100_000_000,
        extensions: frozenset[str] = TEXT_EXTENSIONS,
    ) -> None:
        self.root = Path(repo_path).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"repository path is not a directory: {self.root}")
        if min(max_file_size, max_files, max_total_size) <= 0:
            raise ValueError("scanner limits must be positive")
        self.max_file_size = max_file_size
        self.max_files = max_files
        self.max_total_size = max_total_size
        self.extensions = frozenset(extension.lower() for extension in extensions)
        self.scan_complete = True

    def scan(self) -> list[RepoFile]:
        return list(self.iter_files())

    def iter_files(self) -> Iterator[RepoFile]:
        self.scan_complete = True
        yielded = 0
        total_size = 0
        for current, directories, filenames in os.walk(self.root, topdown=True, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory.lower() not in EXCLUDED_PARTS
                and not (Path(current) / directory).is_symlink()
            )
            for filename in sorted(filenames):
                if yielded >= self.max_files:
                    self.scan_complete = False
                    return
                path = Path(current) / filename
                relative = path.relative_to(self.root)
                if self._excluded(relative) or path.suffix.lower() not in self.extensions or path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                    if stat.st_size > self.max_file_size:
                        continue
                    if total_size + stat.st_size > self.max_total_size:
                        self.scan_complete = False
                        continue
                    data = path.read_bytes()
                except (OSError, PermissionError):
                    continue
                if (
                    len(data) > self.max_file_size
                    or b"\x00" in data[:8192]
                ):
                    continue
                if total_size + len(data) > self.max_total_size:
                    self.scan_complete = False
                    continue
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                yielded += 1
                total_size += len(data)
                relative_path = relative.as_posix()
                yield RepoFile(
                    path=relative_path,
                    content=redact_secrets(content),
                    content_hash=hashlib.sha256(data).hexdigest(),
                    size=len(data),
                    title=self._title(relative_path, content),
                )

    @staticmethod
    def _excluded(path: Path) -> bool:
        lowered = {part.lower() for part in path.parts}
        filename = path.name.lower()
        return bool(lowered & EXCLUDED_PARTS) or filename.startswith(".env") or path.stem.lower() in {
            "env",
            "secrets",
        }

    @staticmethod
    def _title(path: str, content: str) -> str:
        if Path(path).suffix.lower() == ".md":
            for line in content.splitlines():
                match = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", line)
                if match:
                    return match.group(1)
        return Path(path).name
