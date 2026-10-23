from __future__ import annotations

from mlops_agents.agents.runtime.compaction import compact_if_needed
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.compaction_fakes import MockSummaryCaller, make_model


async def test_compaction_uses_reported_usage_from_llm_calls(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("New summary")
    # Context window 300 tokens -> 75% is 225 tokens
    model = make_model(context_window=300)

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
    await compact_if_needed(
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text="Sys",
        turn_id=t2,
        agent_name="orchestrator",
    )

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
    model = make_model(context_window=200)

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
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text="Short system",
        turn_id=t2,
        agent_name="orchestrator",
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
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history2,
        system_text="Short system",
        turn_id=t3,
        agent_name="orchestrator",
    )

    # Compaction must NOT have fired again! caller.calls remains 1
    assert len(caller.calls) == 1
