from __future__ import annotations

import asyncio

from app.agent.context import ContextManager
from app.agent.deepseek import StreamDelta
from app.agent.orchestrator import AgentOrchestrator
from app.config import Settings
from app.crud import ConversationService
from app.schemas import ConversationCreate


class FakeLlm:
    def __init__(self) -> None:
        self.summary_calls = 0

    async def stream_chat(self, messages, *, model, max_tokens):
        yield StreamDelta(text="Ответ", usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})

    async def complete(self, messages, *, model, max_tokens):
        self.summary_calls += 1
        return f"summary-{self.summary_calls}", {"total_tokens": 20}


class EmptyRetriever:
    def search(self, *args, **kwargs):
        return []


class NoopMcp:
    async def call(self, *args, **kwargs):
        raise AssertionError("MCP is not expected for an ordinary chat message")


def test_summary_updates_on_tenth_and_twentieth_user_request(db) -> None:
    async def exercise() -> None:
        service = ConversationService(db)
        conversation = service.create(ConversationCreate(title="Memory"))
        llm = FakeLlm()
        agent = AgentOrchestrator(
            db,
            Settings(auto_index=False),
            llm,
            EmptyRetriever(),
            NoopMcp(),
        )

        for request_number in range(1, 21):
            events = [event async for event in agent.chat(conversation.id, f"обычный вопрос {request_number}")]
            assert any("event: done" in event for event in events)

        db.refresh(conversation)
        assert conversation.user_message_count == 20
        assert conversation.summary == "summary-2"
        assert conversation.summarized_through_message_id is not None
        assert llm.summary_calls == 2
        assistants = [message for message in service.list_messages(conversation.id) if message.role.value == "assistant"]
        assert assistants[9].summary_tokens == 20
        assert assistants[19].summary_tokens == 20

    asyncio.run(exercise())


def test_context_budget_drops_retrieval_before_recent_history() -> None:
    class Chunk:
        def __init__(self, content: str) -> None:
            self.content = content
            self.metadata = {"path": "README.md", "line_start": 1, "line_end": 2}

    history = [
        {"role": "user", "content": "short question"},
        {"role": "assistant", "content": "short answer"},
    ]
    plan = ContextManager().build(
        system_prompt="system",
        current_message="current",
        summary="summary",
        history=history,
        rag=[Chunk("x" * 20_000), Chunk("useful small chunk")],
        tool_context=[],
        context_window=4_096,
        max_output_tokens=1_024,
    )

    assert plan.estimated_input_tokens + plan.output_reserve < plan.limit
    assert plan.history_tokens > 0
    assert [chunk.content for chunk in plan.rag] == ["useful small chunk"]
