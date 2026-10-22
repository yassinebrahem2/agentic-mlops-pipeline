from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from mlops_agents.agents.runtime.compaction import compact_if_needed
from mlops_agents.agents.runtime.contract import TextPart, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases


class MockSummaryCaller(ModelCaller):
    def __init__(self, summary_text: str = "Summary of earlier turns") -> None:
        self.summary_text = summary_text
        self.calls: list[dict[str, Any]] = []

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
    ) -> ModelReply:
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        return ModelReply(
            message=AIMessage(content=self.summary_text),
            prompt_tokens=50,
            completion_tokens=20,
        )


def _make_model(context_window: int = 1000) -> ResolvedModel:
    return ResolvedModel(
        address="fake:m",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="m",
        context_window=context_window,
        keys=(),
    )


async def test_compaction_skips_when_two_or_fewer_turns(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller()
    model = _make_model(100)  # Very small context window

    session_id = await store.session_id("prj_1", "orchestrator")
    # Only 2 turns
    m1 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
    )
    t1 = await store.add_turn("prj_1", session_id, m1.id, "done")
    m2 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t1,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="Hi there")],
        status="complete",
    )
    m3 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Second question")],
        status="complete",
    )
    history = [m1, m2, m3]

    await compact_if_needed(
        store, caller, model, "prj_1", session_id, history, system_text="System prompt"
    )

    # Caller never called because <= 2 turns
    assert len(caller.calls) == 0
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary is None
    assert compacted_through is None


async def test_compaction_triggered_when_size_exceeds_threshold(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("Key decisions: decided on A and B.")
    # Context window 200 tokens -> 75% is 150 tokens
    model = _make_model(context_window=200)

    session_id = await store.session_id("prj_1", "orchestrator")

    # Turn 1: 500 characters (~125 tokens)
    m1 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="User question 1: " + "a" * 200)],
        status="complete",
    )
    t1 = await store.add_turn("prj_1", session_id, m1.id, "done")
    m2 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t1,
        author_kind="agent",
        author_name="orchestrator",
        parts=[
            ToolCallPart(type="tool_call", id="c1", name="Read", input={"path": "README.md"}),
            ToolResultPart(
                type="tool_result",
                tool_call_id="c1",
                content="result: " + "b" * 200,
                is_error=False,
            ),
            TextPart(type="text", text="Agent answer 1"),
        ],
        status="complete",
    )

    # Turn 2:
    m3 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="User question 2: " + "c" * 200)],
        status="complete",
    )
    t2 = await store.add_turn("prj_1", session_id, m3.id, "done")
    m4 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t2,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="Agent answer 2")],
        status="complete",
    )

    # Turn 3:
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="User question 3")],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    system_text = "System prompt: " + "s" * 100

    await compact_if_needed(
        store, caller, model, "prj_1", session_id, history, system_text=system_text
    )

    # 1 compaction call was made with tools=[]
    assert len(caller.calls) == 1
    assert caller.calls[0]["tools"] == []

    # Keeps last 2 whole turns (Turn 2 and Turn 3)
    # Turn 1 is summarized -> compacted_through is m2.id (last message of Turn 1)
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary == "Key decisions: decided on A and B."
    assert compacted_through == m2.id


async def test_compaction_uses_reported_usage_from_llm_calls(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("New summary")
    # Context window 300 tokens -> 75% is 225 tokens
    model = _make_model(context_window=300)

    session_id = await store.session_id("prj_1", "orchestrator")

    # Turn 1
    m1 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q1")],
        status="complete",
    )
    t1 = await store.add_turn("prj_1", session_id, m1.id, "done")
    m2 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t1,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A1")],
        status="complete",
    )

    # Record LLM call for Turn 1 with 200 prompt tokens
    await store.add_llm_call(
        "prj_1",
        t1,
        agent="orchestrator",
        model_address="fake:m",
        prompt_tokens=200,
        completion_tokens=50,
        latency_ms=100,
    )

    # Turn 2
    m3 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q2 " + "x" * 120)],  # ~30 tokens
        status="complete",
    )
    t2 = await store.add_turn("prj_1", session_id, m3.id, "done")
    m4 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t2,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A2")],
        status="complete",
    )

    # Turn 3
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q3")],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    # Total estimate: 200 + (len(m3..m5) / 4) >= 230 > 225
    await compact_if_needed(store, caller, model, "prj_1", session_id, history, system_text="Sys")

    assert len(caller.calls) == 1
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary == "New summary"
    assert compacted_through == m2.id


async def test_compaction_without_reported_usage_does_not_fire_every_turn(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("Initial summary")
    # Context window 200 tokens -> 75% is 150 tokens
    model = _make_model(context_window=200)

    session_id = await store.session_id("prj_1", "orchestrator")

    # Turn 1: Long message (~175 tokens)
    m1 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q1 " + "a" * 700)],
        status="complete",
    )
    t1 = await store.add_turn("prj_1", session_id, m1.id, "done")
    m2 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t1,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A1")],
        status="complete",
    )

    # Turn 2: Short
    m3 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q2")],
        status="complete",
    )
    t2 = await store.add_turn("prj_1", session_id, m3.id, "done")
    m4 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t2,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A2")],
        status="complete",
    )

    # Turn 3: Short
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q3")],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    # First compaction: no usage reported, total length exceeds 150 tokens
    await compact_if_needed(
        store, caller, model, "prj_1", session_id, history, system_text="Short system"
    )

    assert len(caller.calls) == 1
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary == "Initial summary"
    assert compacted_through == m2.id

    # Now Turn 4 arrives with another short message
    t3 = await store.add_turn("prj_1", session_id, m5.id, "done")
    m6 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t3,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A3")],
        status="complete",
    )
    m7 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q4")],
        status="complete",
    )

    history2 = [m1, m2, m3, m4, m5, m6, m7]
    # Second check: no reported usage, but messages after compacted_through are only m3..m7 (very short)
    await compact_if_needed(
        store, caller, model, "prj_1", session_id, history2, system_text="Short system"
    )

    # Compaction must NOT have fired again! caller.calls remains 1
    assert len(caller.calls) == 1
