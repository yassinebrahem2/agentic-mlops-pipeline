from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiosqlite
import httpx2
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime import executor as executor_module
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hooks import RepositoryOnlyWorkspaces
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, stage1_hooks
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes import fake_openai as fake_openai_module
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


class SlowDummyArgs(BaseModel):
    pass


class SlowDummyTool(AgentTool[SlowDummyArgs]):
    name = "SlowTool"
    description = "Slow dummy tool."
    risk = RiskClass.READ
    args_model = SlowDummyArgs

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, ctx: ToolContext, args: SlowDummyArgs) -> ToolOutput:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the turn is cancelled")


class EndStateSink(RecordingSink):
    """Logs when the stopped end state is published, to order it against other events."""

    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    async def message(self, message: MessageView, *, created: bool) -> None:
        if message.status == "stopped":
            self.log.append("end state")
        await super().message(message, created=created)


async def _assert_thread_deleted(db_path: Path, thread_id: str) -> None:
    conn = await connect_checkpointer(db_path)
    saver = AsyncSqliteSaver(conn)
    chk = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})  # pyright: ignore[reportUnknownMemberType]
    assert chk is None
    await conn.close()


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


def _workspace_svc(
    paths: DataPaths, databases: Databases, keystore: MemoryKeyStore
) -> WorkspaceService:
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    return WorkspaceService(
        _settings(paths), paths, databases, GitRunner(paths.bin, github.token), github, http
    )


async def test_executor_cancellation_during_slow_tool(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def_dir = tmp_path / "defs"
    def_dir.mkdir()
    def_file = def_dir / "slowagent.md"
    def_file.write_text(
        "---\nname: slowagent\ndisplay_name: Slow Agent\ndescription: Test\ntools:\n  - SlowTool\n---\nPrompt\n"
    )

    slow_tool = SlowDummyTool()
    tools = default_registry()
    tools.register(slow_tool)
    catalog = AgentCatalog.load(tools.names(), extra_dirs=[def_dir])

    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http)
    workspaces = RepositoryOnlyWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces)
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_slow", {"README.md": "Hi"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("slowagent", f"{prov.name}:fake-tools")
    session_id = await store.session_id(project.id, "slowagent")

    user_msg = await store.add_message(
        project.id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Run slow tool")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, session_id, user_msg.id, "running")

    monkeypatch.setitem(fake_openai_module.TOOL_ARGS, "SlowTool", {})

    log: list[str] = []

    async def recording_connect(path: Path) -> aiosqlite.Connection:
        conn = await connect_checkpointer(path)
        close = conn.close

        async def recording_close() -> None:
            await close()
            log.append("checkpointer closed")

        monkeypatch.setattr(conn, "close", recording_close)
        return conn

    monkeypatch.setattr(executor_module, "connect_checkpointer", recording_connect)

    sink = EndStateSink(log)
    task = asyncio.create_task(
        executor.run(project.id, session_id, "slowagent", turn_id, sink, AsyncRLock())
    )

    tool_started = asyncio.create_task(slow_tool.started.wait())
    # Racing the run makes a turn that ends before its tool runs fail here instead of hanging.
    done, _ = await asyncio.wait({tool_started, task}, return_when=asyncio.FIRST_COMPLETED)
    tool_started.cancel()
    # Waits for the cancel to land, so no waiter task outlives the test.
    await asyncio.wait({tool_started})
    assert done == {tool_started}
    assert len(sink.tools_started) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    msgs = await store.messages(project.id, session_id)
    agent_msg = next(m for m in msgs if m.author_kind == "agent")
    assert agent_msg.status == "stopped"
    assert len(agent_msg.parts) == 1
    assert agent_msg.parts[0].type == "tool_call"
    assert any(m.status == "stopped" and created is False for m, created in sink.messages)
    assert log == ["checkpointer closed", "end state"]

    await _assert_thread_deleted(paths.project_db(project.id), turn_id)


async def test_executor_cancellation_during_streaming_preserves_partial_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http)
    workspaces = RepositoryOnlyWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces)
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_stream_cancel", {"README.md": "Hi"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")
    session_id = await store.session_id(project.id, "orchestrator")

    user_msg = await store.add_message(
        project.id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Run turn")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, session_id, user_msg.id, "running")

    started_streaming = asyncio.Event()

    async def slow_stream_call(*args: object, **kwargs: object) -> Any:
        on_text = kwargs.get("on_text")
        if on_text is None and len(args) >= 4:
            on_text = args[3]
        if on_text and callable(on_text):
            res = on_text("Streamed prefix before cancel")
            if asyncio.iscoroutine(res):
                await res
        started_streaming.set()
        await asyncio.Event().wait()
        return ModelReply(message=AIMessage(content="never"), prompt_tokens=1, completion_tokens=1)

    monkeypatch.setattr(caller, "call", slow_stream_call)

    sink = RecordingSink()
    run_task = asyncio.create_task(
        executor.run(project.id, session_id, "orchestrator", turn_id, sink, AsyncRLock())
    )

    await started_streaming.wait()
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    msgs = await store.messages(project.id, session_id)
    agent_msg = next(m for m in msgs if m.author_kind == "agent")
    assert agent_msg.status == "stopped"
    assert any(
        isinstance(p, TextPart) and p.text == "Streamed prefix before cancel"
        for p in agent_msg.parts
    )
    assert any(m.status == "stopped" and created is False for m, created in sink.messages)

    await _assert_thread_deleted(paths.project_db(project.id), turn_id)
