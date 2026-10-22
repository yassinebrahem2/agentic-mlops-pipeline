from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy import update

import tests.fakes.fake_openai as fake_openai_module
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentLimitsUpdate, AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


async def _set_project_status(
    databases: Databases, project_id: str, status: str, detail: str | None = None
) -> None:
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(status=status, status_detail=detail)
        )


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


async def _setup_executor_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    files: dict[str, str] | None = None,
    extra_dirs: list[Path] | None = None,
) -> tuple[
    TurnExecutor,
    SessionStore,
    AgentSettingsService,
    WorkspaceService,
    str,
    str,
    str,
    ProvidersService,
    ModelCaller,
]:
    repo_files = files if files is not None else {"README.md": "Line 1 of readme\nLine 2\n"}
    repo_dir = make_repo(tmp_path / "repo", repo_files, git_env)

    tools = default_registry()
    catalog = AgentCatalog.load(tools.names(), extra_dirs=extra_dirs or ())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        git=git,
    )
    executor = TurnExecutor(store=store, caller=caller, builder=builder)

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")
    session_id = await store.session_id(project.id, "orchestrator")

    user_msg = await store.add_message(
        project.id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Check readme")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, session_id, user_msg.id, "running")

    return (
        executor,
        store,
        settings_svc,
        workspace_svc,
        project.id,
        session_id,
        turn_id,
        providers_svc,
        caller,
    )


async def test_executor_fake_tools_on_orchestrator(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    (
        executor,
        _store,
        _,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    sink = RecordingSink()

    end_status, error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink
    )

    assert end_status == "done"
    assert error_text is None

    # Check sink events
    assert len(sink.messages) == 2  # created=True and created=False
    assert sink.messages[0][1] is True
    assert sink.messages[1][1] is False
    assert len(sink.tools_started) == 1
    assert sink.tools_started[0].name == "Read"
    assert len(sink.tools_finished) == 1
    assert not sink.tools_finished[0].is_error
    assert len(sink.texts) > 0

    # Parts order: tool call, tool result, final text
    final_msg = sink.messages[1][0]
    types = [p.type for p in final_msg.parts]
    assert types == ["tool_call", "tool_result", "text"]

    # Final text contains the first line
    final_text_part = final_msg.parts[2]
    assert isinstance(final_text_part, TextPart)
    assert "Line 1 of readme" in final_text_part.text

    # llm_calls has 2 rows with latency > 0
    engine = await databases.project(project_id)
    from sqlalchemy import select

    from mlops_agents.storage.tables import llm_calls

    async with engine.connect() as conn:
        rows = (await conn.execute(select(llm_calls).where(llm_calls.c.turn_id == turn_id))).all()
    assert len(rows) == 2
    for r in rows:
        assert r.latency_ms >= 0


async def test_executor_secret_and_untracked_error_handled(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Repo with .env
    (
        executor,
        _store,
        _,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        files={"README.md": "Hello", ".env": "SECRET=123"},
    )
    # Direct fake model to call Read on .env
    monkeypatch.setitem(fake_openai_module.TOOL_ARGS, "Read", {"file_path": ".env"})

    sink = RecordingSink()
    end_status, error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink
    )

    assert end_status == "done"
    assert error_text is None
    # Tool finished reported error
    assert len(sink.tools_finished) == 1
    assert sink.tools_finished[0].is_error


async def test_executor_step_limit_override(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    (
        executor,
        _store,
        settings_svc,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    # Override max_steps = 1
    await settings_svc.set_project_limits(
        project_id, "orchestrator", AgentLimitsUpdate(max_steps=1)
    )

    sink = RecordingSink()
    end_status, _error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink
    )

    assert end_status == "done"
    final_msg = sink.messages[-1][0]
    last_part = final_msg.parts[-1]
    assert isinstance(last_part, TextPart)
    assert (
        last_part.text
        == "Stopped after 1 steps (this agent's step limit). Send a message to continue."
    )


async def test_executor_refusals(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    (
        executor,
        _store,
        settings_svc,
        _workspace_svc,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    # 1. Project missing refusal
    await _set_project_status(databases, project_id, status="missing", detail="Missing repo")
    sink = RecordingSink()
    status, err = await executor.run(project_id, session_id, "orchestrator", turn_id, sink)
    assert status == "failed"
    assert err == "Missing repo"
    assert len(sink.messages) == 1
    assert sink.messages[0][0].author_kind == "system"

    # 2. Project empty refusal
    await _set_project_status(databases, project_id, status="empty", detail=None)
    sink = RecordingSink()
    status, err = await executor.run(project_id, session_id, "orchestrator", turn_id, sink)
    assert status == "done"
    assert err is None
    assert len(sink.messages) == 1
    assert sink.messages[0][0].author_kind == "agent"
    part_0 = sink.messages[0][0].parts[0]
    assert isinstance(part_0, TextPart)
    assert "no committed files yet" in part_0.text

    # 3. Model unassigned refusal
    await _set_project_status(databases, project_id, status="ok", detail=None)
    await settings_svc.set_default_model("orchestrator", None)
    sink = RecordingSink()
    status, err = await executor.run(project_id, session_id, "orchestrator", turn_id, sink)
    assert status == "failed"
    assert err is not None
    assert "Choose a model" in err
    assert sink.messages[0][0].author_kind == "system"


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
    # Create throwaway definition with SlowTool
    def_dir = tmp_path / "defs"
    def_dir.mkdir()
    def_file = def_dir / "slowagent.md"
    def_file.write_text(
        "---\nname: slowagent\ndisplay_name: Slow Agent\ndescription: Test\ntools:\n  - SlowTool\n---\nPrompt\n"
    )

    tools = default_registry()
    tools.register(SlowDummyTool())
    catalog = AgentCatalog.load(tools.names(), extra_dirs=[def_dir])

    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        git=GitRunner(paths.bin),
    )
    executor = TurnExecutor(store=store, caller=caller, builder=builder)

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

    sink = RecordingSink()
    task = asyncio.create_task(executor.run(project.id, session_id, "slowagent", turn_id, sink))

    # Wait for tool to start
    for _ in range(50):
        if sink.tools_started:
            break
        await asyncio.sleep(0.02)
    assert len(sink.tools_started) == 1

    # Cancel task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Verify message in store has status 'stopped' and no tool result
    msgs = await store.messages(project.id, session_id)
    agent_msg = next(m for m in msgs if m.author_kind == "agent")
    assert agent_msg.status == "stopped"
    assert len(agent_msg.parts) == 1
    assert agent_msg.parts[0].type == "tool_call"


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
    (
        executor,
        store,
        _,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        caller,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    async def fail_call(*args: object, **kwargs: object) -> Any:
        raise TurnFailed("Simulated API failure")

    monkeypatch.setattr(caller, "call", fail_call)

    sink = RecordingSink()
    status, err = await executor.run(project_id, session_id, "orchestrator", turn_id, sink)

    assert status == "failed"
    assert err == "Simulated API failure"

    # Message is marked complete and system message exists
    msgs = await store.messages(project_id, session_id)
    agent_msg = next(m for m in msgs if m.author_kind == "agent")
    assert agent_msg.status == "complete"

    sys_msg = next(m for m in msgs if m.author_kind == "system")
    sys_part = sys_msg.parts[0]
    assert isinstance(sys_part, TextPart)
    assert sys_part.text == "Simulated API failure"


async def test_executor_second_agent_in_tmp_path(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    def_dir = tmp_path / "extra_defs"
    def_dir.mkdir()
    def_file = def_dir / "customagent.md"
    def_file.write_text(
        "---\nname: customagent\ndisplay_name: Custom Agent\ndescription: Test custom agent\ntools:\n  - Read\n---\nYou are custom agent.\n"
    )

    (
        executor,
        store,
        settings_svc,
        _,
        project_id,
        _,
        _,
        _prov_svc,
        _,
    ) = await _setup_executor_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
    )

    await settings_svc.set_default_model("customagent", "fake:fake-tools")
    session_id = await store.session_id(project_id, "customagent")

    user_msg = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hello custom")],
        status="complete",
    )
    turn_id = await store.add_turn(project_id, session_id, user_msg.id, "running")

    sink = RecordingSink()
    status, err = await executor.run(project_id, session_id, "customagent", turn_id, sink)

    assert status == "done"
    assert err is None
    final_msg = sink.messages[-1][0]
    assert any("Line 1 of readme" in p.text for p in final_msg.parts if isinstance(p, TextPart))
