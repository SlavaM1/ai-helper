"""OpenAI-compatible DeepSeek provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass
class StreamDelta:
    text: str = ""
    usage: dict[str, int] | None = None


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str, *, timeout: float = 120.0) -> None:
        self.configured = bool(api_key)
        self.client = AsyncOpenAI(api_key=api_key or "not-configured", base_url=base_url, timeout=timeout)

    def require_key(self) -> None:
        if not self.configured:
            raise RuntimeError("DEEPSEEK_API_KEY не настроен")

    async def models(self) -> list[dict[str, Any]]:
        self.require_key()
        response = await self.client.models.list()
        return [{"id": item.id, "name": item.id, **({"owned_by": item.owned_by} if item.owned_by else {})} for item in response.data]

    async def stream_chat(
        self, messages: list[dict[str, str]], *, model: str, max_tokens: int
    ) -> AsyncIterator[StreamDelta]:
        self.require_key()
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            text = chunk.choices[0].delta.content if chunk.choices else None
            usage = None
            if chunk.usage:
                usage = {
                    "input_tokens": chunk.usage.prompt_tokens,
                    "output_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
            yield StreamDelta(text=text or "", usage=usage)

    async def complete(
        self, messages: list[dict[str, str]], *, model: str, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        return await self._complete(messages, model=model, max_tokens=max_tokens)

    async def complete_json(
        self, messages: list[dict[str, str]], *, model: str, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        return await self._complete(
            messages,
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        response_format: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, int]]:
        self.require_key()
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            **({"response_format": response_format} if response_format else {}),
        )
        usage = response.usage
        if usage is None:
            return response.choices[0].message.content or "", {}
        return response.choices[0].message.content or "", {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
