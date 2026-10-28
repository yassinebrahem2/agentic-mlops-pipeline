from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

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
    def __init__(self, reply: ModelReply) -> None:
        self._reply = reply

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
    ) -> ModelReply:
        return self._reply


async def test_graph_checkpointer_thread_lifecycle_on_done(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = make_agent(repo_dir, paths.bin)
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_chk", "orchestrator")
    user_msg = await store.add_message(
        "prj_chk",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hi")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_chk", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_chk",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    caller = MockModelCaller(
        ModelReply(
            message=AIMessage(content="Done", tool_calls=[]),
            prompt_tokens=10,
            completion_tokens=5,
        )
    )

    db_path = paths.project_db("prj_chk")
    await databases.project("prj_chk")
    conn = await connect_checkpointer(db_path)
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_chk",
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
        await graph.ainvoke(initial_chk_state, config=config)  # pyright: ignore[reportUnknownMemberType]
        tup = await checkpointer.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
        assert tup is not None
    finally:
        await checkpointer.adelete_thread(turn_id)
        await conn.close()

    conn2 = await connect_checkpointer(db_path)
    checkpointer2 = AsyncSqliteSaver(conn2)
    tup_after = await checkpointer2.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
    assert tup_after is None
    await conn2.close()


async def test_graph_checkpointer_thread_lifecycle_on_cancellation(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = make_agent(repo_dir, paths.bin)
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_cancel", "orchestrator")
    user_msg = await store.add_message(
        "prj_cancel",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hi")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_cancel", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_cancel",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    started = asyncio.Event()

    class CancelCaller:
        async def call(
            self,
            model: Any,
            messages: list[Any],
            tools: list[dict[str, Any]],
            on_text: Any,
        ) -> ModelReply:
            started.set()
            await asyncio.Event().wait()
            return ModelReply(
                message=AIMessage(content="never"), prompt_tokens=1, completion_tokens=1
            )

    caller = CancelCaller()
    db_path = paths.project_db("prj_cancel")
    await databases.project("prj_cancel")
    conn = await connect_checkpointer(db_path)
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,  # pyright: ignore[reportArgumentType]
        store=store,
        sink=sink,
        project_id="prj_cancel",
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
        invoke_task = asyncio.create_task(
            graph.ainvoke(initial_chk_state, config=config)  # pyright: ignore[reportUnknownMemberType]
        )
        await started.wait()
        invoke_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invoke_task
    finally:
        await checkpointer.adelete_thread(turn_id)
        await conn.close()

    conn2 = await connect_checkpointer(db_path)
    checkpointer2 = AsyncSqliteSaver(conn2)
    tup_after = await checkpointer2.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
    assert tup_after is None
    await conn2.close()


async def test_graph_checkpointer_thread_lifecycle_on_failure(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = make_agent(repo_dir, paths.bin)
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_fail", "orchestrator")
    user_msg = await store.add_message(
        "prj_fail",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hi")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_fail", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_fail",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    class FailingCaller:
        async def call(
            self,
            model: Any,
            messages: list[Any],
            tools: list[dict[str, Any]],
            on_text: Any,
        ) -> ModelReply:
            raise RuntimeError("API failure")

    caller = FailingCaller()
    db_path = paths.project_db("prj_fail")
    await databases.project("prj_fail")
    conn = await connect_checkpointer(db_path)
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,  # pyright: ignore[reportArgumentType]
        store=store,
        sink=sink,
        project_id="prj_fail",
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
        with pytest.raises(RuntimeError):
            await graph.ainvoke(initial_chk_state, config=config)  # pyright: ignore[reportUnknownMemberType]
    finally:
        await checkpointer.adelete_thread(turn_id)
        await conn.close()

    conn2 = await connect_checkpointer(db_path)
    checkpointer2 = AsyncSqliteSaver(conn2)
    tup_after = await checkpointer2.aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
    assert tup_after is None
    await conn2.close()
