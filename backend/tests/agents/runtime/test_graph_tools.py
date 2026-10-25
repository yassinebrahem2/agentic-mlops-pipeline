from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
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


async def test_graph_tool_not_in_definition_returns_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\n"}, git_env)
    # Agent only has tool "Write", NOT "Read"
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1", tool_names=("Write",))
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

    caller = MockModelCaller(
        [
            ModelReply(
                message=AIMessage(
                    content="",
                    tool_calls=[{"id": "c1", "name": "Read", "args": {"file_path": "README.md"}}],
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Tool was unavailable", tool_calls=[]),
                prompt_tokens=15,
                completion_tokens=4,
            ),
        ]
    )

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
        hooks=graph_hooks(agent),
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
    assert sink.tools_finished[0].is_error is True
    assert sink.tools_finished[0].content == "There is no tool named Read."

    # Parts order: tool_call, tool_result, text
    types = [p.type for p in current_parts]
    assert types == ["tool_call", "tool_result", "text"]
    assert current_parts[1].content == "There is no tool named Read."


async def test_graph_parallel_tools_order(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"a.txt": "A\n", "b.txt": "B\n"}, git_env)
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1", tool_names=("Read",))
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read both")],
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

    caller = MockModelCaller(
        [
            ModelReply(
                message=AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "c1", "name": "Read", "args": {"file_path": "a.txt"}},
                        {"id": "c2", "name": "Read", "args": {"file_path": "b.txt"}},
                    ],
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Both read", tool_calls=[]),
                prompt_tokens=15,
                completion_tokens=4,
            ),
        ]
    )

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
        hooks=graph_hooks(agent),
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read both")],
        "step_count": 0,
        "stopped_reason": None,
    }
    await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    # Stored parts order: call1, call2, result1, result2, text
    types = [p.type for p in current_parts]
    assert types == ["tool_call", "tool_call", "tool_result", "tool_result", "text"]

    # Live events order: call1, call2 started, then result1, result2 finished
    assert len(sink.tools_started) == 2
    assert [c.id for c in sink.tools_started] == ["c1", "c2"]
    assert len(sink.tools_finished) == 2
    assert [r.tool_call_id for r in sink.tools_finished] == ["c1", "c2"]
