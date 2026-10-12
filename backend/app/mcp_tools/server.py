"""Read-only Git MCP server using the official FastMCP implementation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("read-only-git")
TARGET_REPO_PATH = Path(os.environ.get("TARGET_REPO_PATH", ".")).expanduser().resolve()
MAX_LIST_LIMIT = 5_000
MAX_DIFF_BYTES = 1_000_000


def _repo() -> Path:
    if not TARGET_REPO_PATH.is_dir() or not (TARGET_REPO_PATH / ".git").exists():
        raise ValueError("TARGET_REPO_PATH must point to a Git repository")
    return TARGET_REPO_PATH


def _safe_relative_path(value: str | None) -> str | None:
    if value is None or value in {"", "."}:
        return None
    if "\x00" in value or any(character in value for character in "\r\n"):
        raise ValueError("invalid repository path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
        raise ValueError("path must be a safe repository-relative path")
    root = _repo()
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes the target repository")
    return candidate.as_posix().removeprefix("./")


def _git(*arguments: str, max_bytes: int = 2_000_000) -> tuple[bytes, bool]:
    environment = os.environ.copy()
    for name in list(environment):
        if name in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_EXTERNAL_DIFF",
            "GIT_DIFF_OPTS",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "credential.helper=",
            "-c",
            f"safe.directory={_repo()}",
            "-C",
            str(_repo()),
            *arguments,
        ],
        check=False,
        capture_output=True,
        timeout=20,
        env=environment,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "Git command failed")
    truncated = len(result.stdout) > max_bytes
    return result.stdout[:max_bytes], truncated


def _git_text(*arguments: str, max_bytes: int = 2_000_000) -> tuple[str, bool]:
    data, truncated = _git(*arguments, max_bytes=max_bytes)
    return data.decode("utf-8", errors="replace"), truncated


def _redact_remote(remote: str) -> str:
    remote = re.sub(
        r"(?i)(?:token|password|passwd|access_token|oauth_token)=([^&\s]+)",
        r"\1=[REDACTED]",
        remote,
    )
    if "://" in remote:
        parsed = urlsplit(remote)
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    # SCP-style remotes may include a username, but never expose arbitrary userinfo.
    match = re.match(r"(?:[^/@:]+@)?([^/:]+):(.*)", remote)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return remote


def _current_branch() -> str:
    try:
        branch, _ = _git_text("rev-parse", "--abbrev-ref", "HEAD", max_bytes=1_024)
    except RuntimeError:
        branch, _ = _git_text("symbolic-ref", "--short", "HEAD", max_bytes=1_024)
    return branch.strip()


@mcp.tool()
def git_current_branch() -> dict[str, str]:
    """Return the current branch, HEAD commit and repository name."""

    commit, _ = _git_text("rev-parse", "HEAD", max_bytes=1_024)
    return {
        "branch": _current_branch(),
        "commit": commit.strip(),
        "repository": _repo().name,
    }


@mcp.tool()
def git_list_files(limit: int = 500, prefix: str | None = None) -> dict[str, object]:
    """List tracked files, optionally below a safe repository-relative prefix."""

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    safe_prefix = _safe_relative_path(prefix)
    arguments = ["ls-files", "-z", "--"]
    if safe_prefix:
        arguments.append(safe_prefix)
    data, output_truncated = _git(*arguments, max_bytes=10_000_000)
    all_files = [item.decode("utf-8", errors="replace") for item in data.split(b"\x00") if item]
    files = all_files[:limit]
    return {
        "files": files,
        "count": len(files),
        "truncated": output_truncated or len(all_files) > limit,
        "prefix": safe_prefix,
    }


@mcp.tool()
def git_status() -> dict[str, object]:
    """Return branch and porcelain-v1 working-tree status."""

    output, truncated = _git_text("status", "--short", "--branch", "--untracked-files=all", max_bytes=500_000)
    lines = output.splitlines()
    branch = lines[0].removeprefix("## ") if lines and lines[0].startswith("## ") else ""
    return {"branch": branch, "entries": lines[1:] if branch else lines, "truncated": truncated}


@mcp.tool()
def git_diff(max_bytes: int = 100_000, path: str | None = None) -> dict[str, object]:
    """Return an unstaged diff, bounded by bytes and optionally limited to one path."""

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_DIFF_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_DIFF_BYTES}")
    safe_path = _safe_relative_path(path)
    arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--"]
    if safe_path:
        arguments.append(safe_path)
    data, truncated = _git(*arguments, max_bytes=max_bytes)
    return {
        "diff": data.decode("utf-8", errors="replace"),
        "bytes": len(data),
        "truncated": truncated,
        "path": safe_path,
    }


@mcp.tool()
def git_repo_info() -> dict[str, object]:
    """Return stable repository identity without leaking remote credentials."""

    branch = _current_branch()
    try:
        commit, _ = _git_text("rev-parse", "HEAD", max_bytes=1_024)
    except RuntimeError:
        commit = ""
    try:
        remote_value, _ = _git_text("remote", "get-url", "origin", max_bytes=8_192)
    except RuntimeError:
        remote_value = ""
    status, _ = _git_text("status", "--porcelain", max_bytes=1_024)
    remote = _redact_remote(remote_value.strip()) if remote_value else None
    return {
        "name": _repo().name,
        "path": str(_repo()),
        "branch": branch,
        "commit": commit.strip(),
        "remote": remote,
        "dirty": bool(status),
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
