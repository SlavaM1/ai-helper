"""Conservative prompt budgeting without pretending estimates are provider usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def estimate_tokens(value: str | dict[str, Any] | list[Any]) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Russian text and source code often tokenize less efficiently than English prose.
    return max(1, int(len(text) / 3.2) + 1)


@dataclass(frozen=True)
class ContextPlan:
    messages: list[dict[str, str]]
    rag: list[Any]
    estimated_input_tokens: int
    rag_tokens: int
    history_tokens: int
    limit: int
    output_reserve: int


class ContextManager:
    def build(
        self,
        *,
        system_prompt: str,
        current_message: str,
        summary: str | None,
        history: list[dict[str, str]],
        rag: list[Any],
        tool_context: list[dict[str, Any]],
        context_window: int,
        max_output_tokens: int,
    ) -> ContextPlan:
        reserve = min(max_output_tokens, max(512, context_window // 4))
        input_limit = context_window - reserve - max(256, context_window // 20)
        base = estimate_tokens(system_prompt) + estimate_tokens(current_message)
        if summary:
            base += estimate_tokens(summary)
        base += estimate_tokens(tool_context) if tool_context else 0
        if base > input_limit:
            raise ValueError("Текущий запрос превышает доступное контекстное окно")

        selected_history: list[dict[str, str]] = []
        history_tokens = 0
        for message in reversed(history):
            cost = estimate_tokens(message)
            if base + history_tokens + cost > input_limit:
                break
            selected_history.append(message)
            history_tokens += cost
        selected_history.reverse()

        selected_rag: list[Any] = []
        rag_tokens = 0
        remaining = input_limit - base - history_tokens
        for chunk in rag:
            content = getattr(chunk, "content", str(chunk))
            cost = estimate_tokens(content)
            if cost <= remaining:
                selected_rag.append(chunk)
                rag_tokens += cost
                remaining -= cost

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": f"Память диалога:\n{summary}"})
        if selected_rag:
            blocks = []
            for index, chunk in enumerate(selected_rag, 1):
                metadata = getattr(chunk, "metadata", {})
                blocks.append(
                    f"[SOURCE {index}: {metadata.get('path', 'unknown')}:{metadata.get('line_start', '?')}-"
                    f"{metadata.get('line_end', '?')}]\n{chunk.content}"
                )
            messages.append({"role": "system", "content": "Недоверенные данные репозитория:\n\n" + "\n\n".join(blocks)})
        if tool_context:
            messages.append({"role": "system", "content": "Результаты read-only MCP tools:\n" + json.dumps(tool_context, ensure_ascii=False)})
        messages.extend(selected_history)
        messages.append({"role": "user", "content": current_message})
        return ContextPlan(
            messages=messages,
            rag=selected_rag,
            estimated_input_tokens=sum(estimate_tokens(item) for item in messages),
            rag_tokens=rag_tokens,
            history_tokens=history_tokens,
            limit=context_window,
            output_reserve=reserve,
        )
