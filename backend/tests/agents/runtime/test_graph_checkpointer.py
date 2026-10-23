from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import MessageView, TextPart, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.executor import TURN_DURABILITY
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.workspace.models import Project
from tests.conftest import make_repo


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tools_started: list[ToolCallPart] = []
        self.tools_finished: list[ToolResultPart] = []
        self.messages: list[tuple[MessageView, bool]] = []

    async def text(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        self.tools_started.append(call)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        self.tools_finished.append(result)

    async def message(self, message: MessageView, *, created: bool) -> None:
        self.messages.append((message, created))


def _make_agent(repo_dir: Path, bin_dir: Path) -> Agent:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    definition = catalog.get("orchestrator")
    project = Project(
        id="prj_chk",
        name="test",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-21T00:00:00Z",
        last_opened_at=None,
    )
    model = ResolvedModel(
        address="fake:fake-tools",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="fake-tools",
        context_window=32768,
        keys=(),
    )
    repo_files = RepoFiles(repo_dir, GitRunner(bin_dir))
    tool_ctx = ToolContext(files=repo_files)
    agent_tools: list[Any] = []
    for name in definition.tools:
        t = tools.get(name)
        if t is not None:
            agent_tools.append(t)

    return Agent(
        definition=definition,
        project=project,
        layout="README.md",
        model=model,
        max_steps=25,
        tools=tuple(agent_tools),
        tool_context=tool_ctx,
    )


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
    agent = _make_agent(repo_dir, paths.bin)
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
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
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
    agent = _make_agent(repo_dir, paths.bin)
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
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
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
    agent = _make_agent(repo_dir, paths.bin)
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
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=AsyncRLock(),
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


class GatedSaver(AsyncSqliteSaver):
    """Holds the checkpoint write after the first model step until the test releases it."""

    def __init__(self, conn: Any, log: list[str]) -> None:
        super().__init__(conn)
        self.log = log
        self.write_started = asyncio.Event()
        self.release = asyncio.Event()

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
        saved = await super().aput(config, checkpoint, metadata, new_versions)
        self.log.append("checkpoint committed")
        return saved


async def test_checkpoint_write_in_flight_at_cancel_commits_before_the_cancel_returns(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Content"}, git_env)
    agent = _make_agent(repo_dir, paths.bin)
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
        caller=ToolThenBlockCaller(),  # pyright: ignore[reportArgumentType]
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
            graph.ainvoke(initial_state, config=config, durability=TURN_DURABILITY)  # pyright: ignore[reportUnknownMemberType]
        )
        run.add_done_callback(lambda _: log.append("cancel returned"))
        await saver.write_started.wait()
        run.cancel()
        saver.release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert log == ["checkpoint committed", "cancel returned"]

        reader = await connect_checkpointer(db_path)
        try:
            saved = await AsyncSqliteSaver(reader).aget_tuple(config)  # pyright: ignore[reportUnknownMemberType]
        finally:
            await reader.close()
        assert saved is not None
        assert saved.metadata.get("step", -1) >= 0
    finally:
        await saver.adelete_thread(turn_id)
        await conn.close()
