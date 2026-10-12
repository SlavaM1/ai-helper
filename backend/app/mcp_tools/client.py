"""Small official-MCP client that launches the Git server over stdio."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class GitMcpClient:
    def __init__(
        self,
        target_repo_path: str | os.PathLike[str],
        *,
        command: str | None = None,
        server_module: str = "app.mcp_tools.server",
    ) -> None:
        self.target_repo_path = Path(target_repo_path).expanduser().resolve(strict=True)
        if not self.target_repo_path.is_dir():
            raise ValueError("target_repo_path must be a directory")
        self.command = command or sys.executable
        self.server_module = server_module

    def _parameters(self) -> StdioServerParameters:
        backend_path = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(backend_path) + (os.pathsep + python_path if python_path else "")
        environment["TARGET_REPO_PATH"] = str(self.target_repo_path)
        return StdioServerParameters(
            command=self.command,
            args=["-m", self.server_module],
            env=environment,
            cwd=str(backend_path),
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        async with stdio_client(self._parameters()) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    tool.model_dump(mode="json") if hasattr(tool, "model_dump") else dict(tool)
                    for tool in result.tools
                ]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        try:
            async with stdio_client(self._parameters()) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments or {})
                    if getattr(result, "isError", False):
                        message = "\n".join(getattr(item, "text", str(item)) for item in result.content)
                        raise RuntimeError(message)
                    values: list[Any] = []
                    for item in result.content:
                        text = getattr(item, "text", None)
                        if text is None:
                            values.append(item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item))
                            continue
                        try:
                            values.append(json.loads(text))
                        except json.JSONDecodeError:
                            values.append(text)
                    if len(values) == 1:
                        return values[0]
                    return values
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(f"MCP tool {name!r} failed: {self._error_text(exc)}") from None

    @classmethod
    def _error_text(cls, exc: BaseException) -> str:
        nested = getattr(exc, "exceptions", None)
        if nested:
            return "; ".join(cls._error_text(item) for item in nested)
        return str(exc)

    call_tool = call
