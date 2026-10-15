"""Official FastMCP stdio server confined to TARGET_REPO_PATH workspace."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .files import Files

mcp = FastMCP("workspace-files")


def _files() -> Files:
    root = os.environ.get("TARGET_REPO_PATH")
    if not root:
        raise ValueError("TARGET_REPO_PATH must be an explicitly managed workspace")
    return Files(root)


@mcp.tool()
def file_list(path: str = ".", limit: int = 500, pattern: str = "*", exclude: list[str] | None = None) -> dict:
    """List allowed regular files relative to the managed workspace, no symlinks."""
    return _files().file_list(path, limit, pattern, exclude)


@mcp.tool()
def file_search(query: str, path: str = ".", limit: int = 100, case_sensitive: bool = False) -> dict:
    """Literal search of bounded redacted UTF-8 text in the workspace."""
    return _files().file_search(query, path, limit, case_sensitive)


@mcp.tool()
def file_read(path: str, start_line: int = 1, end_line: int | None = None, max_lines: int = 500) -> dict:
    """Read numbered redacted lines; sha256 is of original, unredacted bytes."""
    return _files().file_read(path, start_line, end_line, max_lines)


@mcp.tool()
def file_create(path: str, content: str) -> dict:
    """Atomically create a bounded UTF-8 file, never overwrite an existing path."""
    return _files().file_create(path, content)


@mcp.tool()
def file_apply_patch(path: str, expected_hash: str, old_text: str, new_text: str) -> dict:
    """Exact UNIQUE text replacement, not unified diff: requires current original SHA-256."""
    return _files().file_apply_patch(path, expected_hash, old_text, new_text)


@mcp.tool()
def file_diff(path: str | None = None, max_bytes: int = 100_000) -> dict:
    """Redacted bounded diff against HEAD including untracked files and line counts."""
    return _files().file_diff(path, max_bytes)


@mcp.tool()
def file_status() -> dict:
    """Workspace change metadata and persistent HEAD-relative byte budget usage."""
    return _files().file_status()


@mcp.tool()
def file_validate(path: str) -> dict:
    """Non-executing Markdown checks or Java lexical heuristic; never a compiler."""
    return _files().file_validate(path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
