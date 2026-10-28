from __future__ import annotations

from mlops_agents.agents.runtime.compaction import compact_if_needed
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.compaction_fakes import MockSummaryCaller, make_model


async def test_compaction_skips_when_two_or_fewer_turns(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller()
    model = make_model(100)  # Very small context window

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
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text="System prompt",
        turn_id=t1,
        agent_name="orchestrator",
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
    model = make_model(context_window=200)

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
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text=system_text,
        turn_id=t2,
        agent_name="orchestrator",
    )

    # 1 compaction call was made with tools=[]
    assert len(caller.calls) == 1
    assert caller.calls[0]["tools"] == []

    # Keeps last 2 whole turns (Turn 2 and Turn 3)
    # Turn 1 is summarized -> compacted_through is m2.id (last message of Turn 1)
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary == "Key decisions: decided on A and B."
    assert compacted_through == m2.id

    # The summary call is reported like any other model call.
    last_call = await store.last_llm_call_prompt_tokens("prj_1", session_id)
    assert last_call is not None
    assert last_call[0] == 50
    assert last_call[2] == t2


APPROVAL_BODY = "PREVIEW-BODY " * 800  # ~10,000 characters


def _approval() -> ApprovalPart:
    return ApprovalPart(
        type="approval",
        approval_id="apr_1",
        tool_call_id="c1",
        preview=Preview(kind="diff", title="Edit README.md", body=APPROVAL_BODY),
        status="approved",
        note="APPROVAL-NOTE",
    )


def _stall() -> ApprovalPart:
    return ApprovalPart(
        type="approval",
        approval_id="apr_2",
        tool_call_id=None,
        preview=Preview(kind="text", title="Stalled: no usable key", body=APPROVAL_BODY),
        status="approved",
        note="STALL-NOTE",
        kind="stall",
        step=1,
    )


async def _three_turns(
    store: SessionStore, session_id: str, filler: str
) -> tuple[list[MessageView], str]:
    """Three turns; the first agent message holds an approval between its call and result."""
    history: list[MessageView] = []
    turn_id = ""
    for n in range(3):
        user = await store.add_message(
            "prj_1",
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text=f"Question {n} {filler}")],
            status="complete",
        )
        history.append(user)
        if n == 2:
            break
        turn_id = await store.add_turn("prj_1", session_id, user.id, "done")
        parts: list[Part] = [TextPart(type="text", text=f"Answer {n}")]
        if n == 0:
            parts = [
                ToolCallPart(type="tool_call", id="c1", name="Edit", input={"path": "README.md"}),
                _approval(),
                ToolResultPart(type="tool_result", tool_call_id="c1", content="ok", is_error=False),
                _stall(),
                *parts,
            ]
        agent = await store.add_message(
            "prj_1",
            session_id,
            turn_id=turn_id,
            author_kind="agent",
            author_name="orchestrator",
            parts=parts,
            status="complete",
        )
        history.append(agent)
    return history, turn_id


async def test_compaction_does_not_count_an_approval_toward_the_size(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller()
    session_id = await store.session_id("prj_1", "orchestrator")
    history, turn_id = await _three_turns(store, session_id, filler="")

    # Counted, the approval's ~2,500 tokens would pass 75% of this 1,000-token window.
    await compact_if_needed(
        store,
        caller,
        make_model(context_window=1000),
        "prj_1",
        session_id,
        history,
        system_text="System prompt",
        turn_id=turn_id,
        agent_name="orchestrator",
    )

    assert caller.calls == []


async def test_compaction_leaves_an_approval_out_of_the_summary_prompt(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller()
    session_id = await store.session_id("prj_1", "orchestrator")
    history, turn_id = await _three_turns(store, session_id, filler="q" * 400)

    await compact_if_needed(
        store,
        caller,
        make_model(context_window=200),
        "prj_1",
        session_id,
        history,
        system_text="System prompt",
        turn_id=turn_id,
        agent_name="orchestrator",
    )

    assert len(caller.calls) == 1
    prompt = str(caller.calls[0]["messages"][-1].content)
    assert "Agent tool call: Edit" in prompt
    assert "Tool result: ok" in prompt
    assert "PREVIEW-BODY" not in prompt
    assert "APPROVAL-NOTE" not in prompt
    assert "apr_1" not in prompt
    assert "STALL-NOTE" not in prompt
    assert "apr_2" not in prompt
