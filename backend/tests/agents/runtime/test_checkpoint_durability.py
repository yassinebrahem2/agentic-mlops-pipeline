from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.executor import TURN_DURABILITY
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.graph_env import RecordingSink, make_agent
from tests.conftest import make_repo


class GatedSaver(AsyncSqliteSaver):
    """Holds the checkpoint write after the first model step until the test releases it."""

    def __init__(self, conn: aiosqlite.Connection, log: list[str]) -> None:
        super().__init__(conn)
        self.log = log
        self.write_started = asyncio.Event()
        self.release = asyncio.Event()
        # The run goes on while the write is held, so later checkpoints can commit after it;
        # the held write's own config (with its checkpoint id) is how the test reads it back.
        self.held: RunnableConfig | None = None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if metadata.get("step") != 0:
            return await super().aput(config, checkpoint, metadata, new_versions)
        self.write_started.set()
        await self.release.wait()
        self.held = await super().aput(config, checkpoint, metadata, new_versions)
        self.log.append("checkpoint committed")
        return self.held


async def test_checkpoint_write_in_flight_at_cancel_commits_before_the_cancel_returns(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = make_agent(repo_dir, paths.bin)
    store = SessionStore(databases)
    session_id = await store.session_id("prj_durable", "orchestrator")
    user_msg = await store.add_message(
        "prj_durable",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hi")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_durable", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_durable",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    class ToolThenBlockCaller:
        """Step 0 asks for a tool, so the run goes on past its checkpoint; step 1 never ends."""

        def __init__(self) -> None:
            self.calls = 0

        async def call(
            self,
            model: Any,
            messages: list[Any],
            tools: list[dict[str, Any]],
            on_text: Any,
        ) -> ModelReply:
            self.calls += 1
            if self.calls == 1:
                call: ToolCall = {"id": "call_1", "name": "NoSuchTool", "args": {}}
                return ModelReply(
                    message=AIMessage(content="", tool_calls=[call]),
                    prompt_tokens=1,
                    completion_tokens=1,
                )
            await asyncio.Event().wait()
            raise AssertionError("unreachable: the turn is cancelled")

    db_path = paths.project_db("prj_durable")
    await databases.project("prj_durable")
    conn = await connect_checkpointer(db_path)
    log: list[str] = []
    saver = GatedSaver(conn, log)
    await saver.setup()
    graph = create_turn_graph(
        agent=agent,
        caller=ToolThenBlockCaller(),  # pyright: ignore[reportArgumentType]  # test double with ModelCaller's call signature only
        store=store,
        sink=RecordingSink(),
        project_id="prj_durable",
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=[],
        lock=AsyncRLock(),
        checkpointer=saver,
    )
    config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
    initial_state: TurnState = {
        "messages": [HumanMessage(content="Hi")],
        "step_count": 0,
        "stopped_reason": None,
    }
    try:
        # The executor's own arguments, so this pins the durability the turn runs with.
        run = asyncio.create_task(
            graph.ainvoke(initial_state, config=config, durability=TURN_DURABILITY)  # pyright: ignore[reportUnknownMemberType]  # LangGraph ainvoke return type is partially untyped
        )
        run.add_done_callback(lambda _: log.append("cancel returned"))
        write_started = asyncio.create_task(saver.write_started.wait())
        # Racing the run makes a run that ends without a step-0 write fail here instead of hanging.
        done, _ = await asyncio.wait({write_started, run}, return_when=asyncio.FIRST_COMPLETED)
        write_started.cancel()
        # Waits for the cancel to land, so no waiter task outlives the test.
        await asyncio.wait({write_started})
        assert done == {write_started}
        run.cancel()
        saver.release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert log == ["checkpoint committed", "cancel returned"]

        assert saver.held is not None
        reader = await connect_checkpointer(db_path)
        try:
            saved = await AsyncSqliteSaver(reader).aget_tuple(saver.held)  # pyright: ignore[reportUnknownMemberType]  # LangGraph aget_tuple is partially untyped
        finally:
            await reader.close()
        assert saved is not None
        assert saved.metadata.get("step") == 0
    finally:
        await saver.adelete_thread(turn_id)
        await conn.close()
