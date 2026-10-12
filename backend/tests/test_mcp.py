from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from app.mcp_tools import GitMcpClient


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('before')\n")
    (repo / "README.md").write_text("# MCP test\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://user:top-secret@github.com/example/private.git?access_token=hidden",
    )
    (repo / "src" / "main.py").write_text("print('after')\n")
    return repo


def test_official_stdio_protocol_tools_and_security(tmp_path: Path) -> None:
    async def exercise() -> None:
        repo = make_repo(tmp_path)
        client = GitMcpClient(repo)
        tools = await client.list_tools()
        names = {tool["name"] for tool in tools}
        assert names == {
            "git_current_branch",
            "git_list_files",
            "git_status",
            "git_diff",
            "git_repo_info",
        }

        branch = await client.call("git_current_branch")
        assert branch["branch"] in {"main", "master"}
        files = await client.call("git_list_files", {"limit": 10, "prefix": "src"})
        assert files["files"] == ["src/main.py"]
        status = await client.call("git_status")
        assert any("src/main.py" in entry for entry in status["entries"])
        diff = await client.call("git_diff", {"max_bytes": 30, "path": "src/main.py"})
        assert diff["bytes"] <= 30
        assert diff["truncated"] is True

        info = await client.call("git_repo_info")
        assert "top-secret" not in (info["remote"] or "")
        assert "hidden" not in (info["remote"] or "")
        assert info["remote"].startswith("https://github.com/example/private.git")

        with pytest.raises(RuntimeError):
            await client.call("git_diff", {"path": "../outside"})
        with pytest.raises(RuntimeError):
            await client.call("git_list_files", {"prefix": ".git"})
        with pytest.raises(RuntimeError):
            await client.call("git_diff", {"max_bytes": 1_000_001})

    asyncio.run(exercise())
