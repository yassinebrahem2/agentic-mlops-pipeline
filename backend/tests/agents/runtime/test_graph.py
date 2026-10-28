from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.graph_env import RecordingSink, graph_hooks, make_agent
from tests.conftest import make_repo


class MockModelCaller(ModelCaller):
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.call_count = 0

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
    ) -> ModelReply:
        self.call_count += 1
        reply = self._replies.pop(0)
        if reply.message.content and isinstance(reply.message.content, str):
            res = on_text(reply.message.content)
            if asyncio.iscoroutine(res):
                await res
        return reply


async def test_graph_loops_model_to_tools_to_model(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\nLine 2"}, git_env)
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1")
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read file")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_1", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_1",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    # 1st reply: calls tool Read
    call_1 = ModelReply(
        message=AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "Read", "args": {"file_path": "README.md"}}],
        ),
        prompt_tokens=10,
        completion_tokens=5,
    )
    # 2nd reply: answers with text
    call_2 = ModelReply(
        message=AIMessage(content="README says Line 1", tool_calls=[]),
        prompt_tokens=15,
        completion_tokens=8,
    )
    caller = MockModelCaller([call_1, call_2])

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
        hooks=graph_hooks(agent, databases),
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read file")],
        "step_count": 0,
        "stopped_reason": None,
    }

    await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    assert caller.call_count == 2
    assert len(sink.tools_started) == 1
    assert sink.tools_started[0].name == "Read"
    assert len(sink.tools_finished) == 1
    assert not sink.tools_finished[0].is_error
    assert "Line 1" in sink.tools_finished[0].content
    assert sink.texts == ["README says Line 1"]

    # Parts order: tool call, tool result, text
    types = [p.type for p in current_parts]
    assert types == ["tool_call", "tool_result", "text"]


async def test_graph_step_limits_enforcement(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\n"}, git_env)
    # max_steps = 1
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1", max_steps=1)
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read file")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_1", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_1",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    # 1st reply: calls tool Read (step 1)
    call_1 = ModelReply(
        message=AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "Read", "args": {"file_path": "README.md"}}],
        ),
        prompt_tokens=10,
        completion_tokens=5,
    )
    # 2nd reply: should NOT be called because step limit reached
    call_2 = ModelReply(
        message=AIMessage(content="Should not be called", tool_calls=[]),
        prompt_tokens=10,
        completion_tokens=5,
    )
    caller = MockModelCaller([call_1, call_2])

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
        hooks=graph_hooks(agent, databases),
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read file")],
        "step_count": 0,
        "stopped_reason": None,
    }

    final_state = await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    # Only 1 model call occurred
    assert caller.call_count == 1
    assert final_state.get("stopped_reason") == "max_steps"

    # Last part contains step limit message
    last_part = current_parts[-1]
    assert isinstance(last_part, TextPart)
    assert (
        last_part.text
        == "Stopped after 1 steps (this agent's step limit). Send a message to continue."
    )


async def test_graph_checkpointer_thread_lifecycle(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1")
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hi")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_1", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_1",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    call = ModelReply(
        message=AIMessage(content="Done", tool_calls=[]),
        prompt_tokens=5,
        completion_tokens=2,
    )
    caller = MockModelCaller([call])

    db_path = paths.project_db("prj_1")
    # ensure project db initialized
    await databases.project("prj_1")
    conn = await connect_checkpointer(db_path)
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
        hooks=graph_hooks(agent, databases),
        checkpointer=checkpointer,
    )

    config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
    initial_chk_state: TurnState = {
        "messages": [HumanMessage(content="Hi")],
        "step_count": 0,
        "stopped_reason": None,
    }
    try:
        await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType]
            initial_chk_state,
            config=config,
        )
        tup = await checkpointer.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
        assert tup is not None
    finally:
        await checkpointer.adelete_thread(turn_id)
        await conn.close()
