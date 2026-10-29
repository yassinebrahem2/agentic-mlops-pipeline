from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    TextPart,
)
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, stage1_hooks
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import FakeClock


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
        _settings(paths),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )


class SlowDummyArgs(BaseModel):
    pass


class SlowDummyTool(AgentTool[SlowDummyArgs]):
    name = "SlowTool"
    description = "Sleeps until cancelled"
    risk = RiskClass.READ
    args_model = SlowDummyArgs

    async def run(self, ctx: ToolContext, args: SlowDummyArgs) -> ToolOutput:
        await asyncio.Event().wait()
        return ToolOutput("never")


async def test_executor_unexpected_exception_during_agent_build_fails_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    failover = KeyFailover(FakeClock())
    providers_svc = ProvidersService(databases, keystore, model_http, failover)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http, failover)
    workspaces = FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
        store=store,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces, databases)
    )

    repo_dir = make_repo(tmp_path / "repo_err", {"README.md": "Hi"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
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

    # Simulate unexpected exception during builder.build
    async def failing_build(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Unexpected failure during agent build")

    monkeypatch.setattr(builder, "build", failing_build)

    sink = RecordingSink()
    status, error = await executor.run(
        project.id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )

    assert status == "failed"
    assert error == UNEXPECTED_FAILURE_TEXT
    assert "Unexpected failure during agent build" in caplog.text

    # Turn execution created a system message with the failure
    msgs = await store.messages(project.id, session_id)
    sys_msgs = [m for m in msgs if m.author_kind == "system"]
    assert len(sys_msgs) == 1
    assert sys_msgs[0].status == "complete"
    assert sys_msgs[0].parts[0].type == "text"
    assert sys_msgs[0].parts[0] == TextPart(type="text", text=UNEXPECTED_FAILURE_TEXT)

    # Sink saw the system message created
    assert any(m.author_kind == "system" and created is True for m, created in sink.messages)


async def test_executor_turn_failed_from_caller(
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
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    failover = KeyFailover(FakeClock())
    providers_svc = ProvidersService(databases, keystore, model_http, failover)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http, failover)
    workspaces = FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
        store=store,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces, databases)
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_fail", {"README.md": "Hi"}, git_env)
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

    async def fail_call(*args: object, **kwargs: object) -> Any:
        on_text = kwargs.get("on_text")
        if on_text is None and len(args) >= 4:
            on_text = args[3]
        if on_text and callable(on_text):
            res = on_text("Partial streamed text before crash")
            if asyncio.iscoroutine(res):
                await res
        raise TurnFailed("Simulated API failure")

    monkeypatch.setattr(caller, "call", fail_call)

    sink = RecordingSink()
    status, err = await executor.run(
        project.id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )

    assert status == "failed"
    assert err == "Simulated API failure"

    msgs = await store.messages(project.id, session_id)
    agent_msg = next(m for m in msgs if m.author_kind == "agent")
    assert agent_msg.status == "complete"
    assert any(
        isinstance(p, TextPart) and p.text == "Partial streamed text before crash"
        for p in agent_msg.parts
    )

    sys_msg = next(m for m in msgs if m.author_kind == "system")
    sys_part = sys_msg.parts[0]
    assert isinstance(sys_part, TextPart)
    assert sys_part.text == "Simulated API failure"

    await _assert_thread_deleted(paths.project_db(project.id), turn_id)
