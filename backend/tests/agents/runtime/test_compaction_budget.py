from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.budget import (
    NOTICE_SUFFIX,
    REQUEST_TOO_LARGE_MESSAGE,
    SUMMARY_MAX_SHARE,
    fixed_tokens,
    room_tokens,
)
from mlops_agents.agents.runtime.compaction import compact_if_needed
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import create_turn_graph
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.compaction_fakes import MockSummaryCaller, make_model
from tests.agents.runtime.graph_env import RecordingSink, graph_hooks
from tests.agents.runtime.test_budget_runtime import (
    _make_custom_agent,  # pyright: ignore[reportPrivateUsage]
)
from tests.conftest import make_repo


async def test_compaction_starts_at_room_share_not_context_window(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("Summary text")
    # Context window 2000 tokens. 75% of window would be 1500 tokens.
    model = make_model(context_window=2000)

    session_id = await store.session_id("prj_1", "orchestrator")

    # Turn 1:
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
        parts=[TextPart(type="text", text="A1 " + "b" * 200)],
        status="complete",
    )

    # Turn 2:
    m3 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q2 " + "c" * 700)],
        status="complete",
    )
    t2 = await store.add_turn("prj_1", session_id, m3.id, "done")
    m4 = await store.add_message(
        "prj_1",
        session_id,
        turn_id=t2,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="A2 " + "d" * 200)],
        status="complete",
    )

    # Turn 3:
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q3 " + "e" * 930)],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    # Total conversation tokens: 2745/4 = 686.25 tokens.
    # Fixed part: system text = 3,200 chars -> 800 tokens.
    # In old code: estimated_size = 800 + 686.25 = 1486.25 <= 75% of context window (1500 tokens).
    # So old code does NOT trigger!
    # In new code: budget = 2000, reserve = 300, fixed = 800 -> room = 2000 - 800 - 300 = 900 tokens.
    # Threshold at COMPACT_AT_SHARE * room = 0.75 * 900 = 675 tokens.
    # 686.25 > 675 -> Compaction MUST trigger!
    system_text = "s" * 3200

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

    assert len(caller.calls) == 1
    summary, compacted_through = await store.summary("prj_1", session_id)
    assert summary == "Summary text"
    assert compacted_through == m2.id


async def test_summary_in_pieces_fits_chunk_budget(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    # Context window: 2000 tokens -> budget 2000.
    # Reserve: 2000 * 0.15 = 300 tokens.
    # Available for summary prompt: 2000 - 300 = 1700 tokens (~6800 chars).
    model = make_model(context_window=2000)
    session_id = await store.session_id("prj_1", "orchestrator")

    caller = MockSummaryCaller("Piece summary")

    # Add 4 turns with 2500 chars each = ~10,000 chars total to summarize (> 6800 chars)
    history: list[MessageView] = []
    for i in range(1, 5):
        mu = await store.add_message(
            "prj_1",
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text=f"Q{i}: " + "u" * 1200)],
            status="complete",
        )
        ti = await store.add_turn("prj_1", session_id, mu.id, "done")
        ma = await store.add_message(
            "prj_1",
            session_id,
            turn_id=ti,
            author_kind="agent",
            author_name="orchestrator",
            parts=[TextPart(type="text", text=f"A{i}: " + "a" * 1200)],
            status="complete",
        )
        history.extend([mu, ma])

    # Add 2 more turns so uncompacted_turns has 6 turns (4 to summarize + last 2 kept)
    for i in range(5, 7):
        mu = await store.add_message(
            "prj_1",
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text=f"Q{i}")],
            status="complete",
        )
        ti = await store.add_turn("prj_1", session_id, mu.id, "done")
        ma = await store.add_message(
            "prj_1",
            session_id,
            turn_id=ti,
            author_kind="agent",
            author_name="orchestrator",
            parts=[TextPart(type="text", text=f"A{i}")],
            status="complete",
        )
        history.extend([mu, ma])

    t_curr = await store.add_turn("prj_1", session_id, history[-1].id, "running")
    await compact_if_needed(
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text="System prompt",
        turn_id=t_curr,
        agent_name="orchestrator",
        force=True,
    )

    # Must have summarized in pieces because 10,000 chars > chunk budget (~6800 chars)
    assert len(caller.calls) >= 2
    for call in caller.calls:
        # Prompt must carry no tools
        assert call["tools"] == []
        # Total prompt chars must fit within (budget - reply_reserve) * 4
        total_prompt_chars = sum(len(str(m.content)) for m in call["messages"])
        assert total_prompt_chars <= (2000 - 300) * 4


async def test_summary_length_capped_at_summary_max_share_of_room(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    # Budget 500 tokens. Reserve = 75 tokens. Fixed = 25 tokens. Room = 400 tokens.
    # SUMMARY_MAX_SHARE = 0.10 -> 40 tokens = 160 characters.
    long_summary = "X" * 600
    caller = MockSummaryCaller(long_summary)
    model = make_model(context_window=500)
    session_id = await store.session_id("prj_1", "orchestrator")

    # 3 turns
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
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q3")],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    system_text = "s" * 100  # fixed = 25 tokens

    t3 = await store.add_turn("prj_1", session_id, m5.id, "running")
    await compact_if_needed(
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text=system_text,
        turn_id=t3,
        agent_name="orchestrator",
        force=True,
    )

    summary, _ = await store.summary("prj_1", session_id)
    assert summary is not None
    room = room_tokens(model, fixed_tokens(system_text, []))
    max_summary_chars = int(room * SUMMARY_MAX_SHARE * 4)
    assert len(summary) <= max_summary_chars


async def test_tool_results_in_summarized_turns_cut_with_tool_result_max_chars(
    paths: DataPaths,
    databases: Databases,
) -> None:
    store = SessionStore(databases)
    caller = MockSummaryCaller("Summary text")
    model = make_model(context_window=2000)
    session_id = await store.session_id("prj_1", "orchestrator")

    # Turn 1 has a very large tool result
    m1 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Run tool")],
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
            ToolCallPart(type="tool_call", id="c1", name="Read", input={"path": "large.txt"}),
            ToolResultPart(
                type="tool_result",
                tool_call_id="c1",
                content="START_OF_OUTPUT " + "z" * 5000 + " END_OF_OUTPUT",
                is_error=False,
            ),
            TextPart(type="text", text="Done"),
        ],
        status="complete",
    )

    # Turns 2 and 3 (the last two turns)
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
    m5 = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Q3")],
        status="complete",
    )

    history = [m1, m2, m3, m4, m5]
    t3 = await store.add_turn("prj_1", session_id, m5.id, "running")
    await compact_if_needed(
        store,
        caller,
        model,
        "prj_1",
        session_id,
        history,
        system_text="System prompt",
        turn_id=t3,
        agent_name="orchestrator",
        force=True,
    )

    assert len(caller.calls) == 1
    human_msg_content = str(caller.calls[0]["messages"][1].content)
    # The tool result must have gone through cut_text and contain NOTICE_SUFFIX
    assert NOTICE_SUFFIX in human_msg_content
    assert "END_OF_OUTPUT" not in human_msg_content


class FakeTooLargeCaller(ModelCaller):
    def __init__(
        self,
        too_large_times: int,
        success_reply: ModelReply | None = None,
        fail_compaction: bool = False,
    ) -> None:
        self.too_large_times = too_large_times
        self.success_reply = success_reply or ModelReply(
            message=AIMessage(content="Final reply"),
            prompt_tokens=50,
            completion_tokens=10,
        )
        self.fail_compaction = fail_compaction
        self.call_count = 0
        self.model_calls = 0
        self.calls: list[dict[str, Any]] = []

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
        on_restart: Any = None,
    ) -> ModelReply:
        self.call_count += 1
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        is_compaction = len(tools) == 0
        if is_compaction and not self.fail_compaction:
            return ModelReply(
                message=AIMessage(content="Compacted summary"),
                prompt_tokens=30,
                completion_tokens=10,
            )
        self.model_calls += 1
        if self.model_calls <= self.too_large_times:
            raise ProviderCallError(
                kind=ProviderErrorKind.REQUEST_TOO_LARGE,
                message="Request too large for context window",
            )
        if self.success_reply.message.content and isinstance(
            self.success_reply.message.content, str
        ):
            res = on_text(self.success_reply.message.content)
            if asyncio.iscoroutine(res):
                await res
        return self.success_reply


async def test_request_too_large_compacts_and_retries_once_and_succeeds(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    agent = _make_custom_agent(repo_dir, paths.bin, fixed_prompt_len=100, request_limit=10000)
    store = SessionStore(databases)
    sink = RecordingSink()
    caller = FakeTooLargeCaller(too_large_times=1)

    session_id = await store.session_id("prj_test", agent.definition.name)

    # Create 3 prior turns
    for i in range(1, 4):
        mu = await store.add_message(
            "prj_test",
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text=f"Q{i} " + "u" * 100)],
            status="complete",
        )
        ti = await store.add_turn("prj_test", session_id, mu.id, "done")
        await store.add_message(
            "prj_test",
            session_id,
            turn_id=ti,
            author_kind="agent",
            author_name=agent.definition.name,
            parts=[TextPart(type="text", text=f"A{i}")],
            status="complete",
        )

    # Current turn
    u_curr = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Current question")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, u_curr.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    # First model call failed with REQUEST_TOO_LARGE, then compacted and retried once, succeeding!
    assert caller.model_calls == 2
    # Verify compaction was executed
    summary, compacted_through = await store.summary("prj_test", session_id)
    assert summary == "Compacted summary"
    assert compacted_through is not None
    # Turn finished successfully
    assert any(isinstance(p, TextPart) and "Final reply" in p.text for p in current_parts)


async def test_request_too_large_retries_once_then_fails_cleanly(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    agent = _make_custom_agent(repo_dir, paths.bin, fixed_prompt_len=100, request_limit=10000)
    store = SessionStore(databases)
    sink = RecordingSink()
    # Always fails with REQUEST_TOO_LARGE
    caller = FakeTooLargeCaller(too_large_times=10)

    session_id = await store.session_id("prj_test", agent.definition.name)

    # Prior turns
    for i in range(1, 4):
        mu = await store.add_message(
            "prj_test",
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text=f"Q{i}")],
            status="complete",
        )
        ti = await store.add_turn("prj_test", session_id, mu.id, "done")
        await store.add_message(
            "prj_test",
            session_id,
            turn_id=ti,
            author_kind="agent",
            author_name=agent.definition.name,
            parts=[TextPart(type="text", text=f"A{i}")],
            status="complete",
        )

    u_curr = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Current question")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, u_curr.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    with pytest.raises(Exception) as exc_info:
        await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]

    # Must retry at most once (model_calls == 2)
    assert caller.model_calls == 2
    # Must fail with REQUEST_TOO_LARGE_MESSAGE and NOT "The turn failed unexpectedly"
    assert REQUEST_TOO_LARGE_MESSAGE in str(exc_info.value)


async def test_last_two_turns_exceeding_budget_have_tool_results_trimmed(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    # Budget 3,000 tokens, fixed part ~100 tokens -> room ~2,450 tokens (~9,800 chars).
    agent = _make_custom_agent(repo_dir, paths.bin, fixed_prompt_len=100, request_limit=3000)
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_test", agent.definition.name)

    # Only 2 turns (turn 1 + current turn 2)
    m1 = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Run large tool")],
        status="complete",
    )
    t1 = await store.add_turn("prj_test", session_id, m1.id, "done")
    # Large tool result: 15,000 characters (~3,750 tokens > room 2,450 tokens!)
    await store.add_message(
        "prj_test",
        session_id,
        turn_id=t1,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[
            ToolCallPart(type="tool_call", id="c1", name="Read", input={"path": "large.txt"}),
            ToolResultPart(
                type="tool_result",
                tool_call_id="c1",
                content="START " + "w" * 15000 + " END",
                is_error=False,
            ),
            TextPart(type="text", text="Tool finished"),
        ],
        status="complete",
    )

    # Turn 2 (current turn)
    u_curr = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Second question")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, u_curr.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    caller = FakeTooLargeCaller(too_large_times=0)

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]

    # Verify model was called with trimmed tool result (contains OLD_TOOL_RESULT_CHARS trim notice)
    assert len(caller.calls) == 1
    sent_messages = caller.calls[0]["messages"]
    tool_messages = [m for m in sent_messages if getattr(m, "tool_call_id", None) == "c1"]
    assert len(tool_messages) == 1
    tool_content = str(tool_messages[0].content)
    assert "characters cut to fit context budget" in tool_content
    assert "END" not in tool_content


class FakeFailingBuilder:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def build(
        self, project_id: str, agent: str, session_id: str = "", turn_id: str = ""
    ) -> Agent:
        return self._agent


async def test_executor_turn_failure_displays_request_too_large_message(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    agent = _make_custom_agent(repo_dir, paths.bin, fixed_prompt_len=100, request_limit=10000)
    store = SessionStore(databases)
    sink = RecordingSink()
    caller = FakeTooLargeCaller(too_large_times=10)

    session_id = await store.session_id("prj_test", agent.definition.name)

    u_curr = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, u_curr.id, "running")

    builder = FakeFailingBuilder(agent)
    executor = TurnExecutor(
        builder=builder,  # pyright: ignore[reportArgumentType]
        caller=caller,
        store=store,
        hooks=graph_hooks(agent, databases),
    )

    outcome = await executor.run(
        project_id="prj_test",
        session_id=session_id,
        agent=agent.definition.name,
        turn_id=turn_id,
        sink=sink,
        lock=asyncio.Lock(),
    )

    status, err_msg = outcome
    assert status == "failed"
    assert err_msg == REQUEST_TOO_LARGE_MESSAGE
    # Confirm message in store is system failure message with REQUEST_TOO_LARGE_MESSAGE
    msgs = await store.messages("prj_test", session_id)
    sys_msgs = [m for m in msgs if m.author_kind == "system"]
    assert len(sys_msgs) == 1
    assert any(
        isinstance(p, TextPart) and REQUEST_TOO_LARGE_MESSAGE in p.text for p in sys_msgs[0].parts
    )
    assert "The turn failed unexpectedly" not in str(sys_msgs[0].parts)
