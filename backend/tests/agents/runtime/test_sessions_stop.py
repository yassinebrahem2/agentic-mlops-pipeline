from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import (
    MessageUpdated,
    StreamEvent,
    TextDelta,
    ToolStarted,
    TurnEnded,
)
from mlops_agents.agents.runtime.sessions import SessionManager
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
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


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


async def _setup_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    tools: Any | None = None,
) -> tuple[SessionManager, WorkspaceService, str, str, Path]:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Hello\n"}, git_env)
    tool_reg = tools if tools is not None else default_registry()
    catalog = AgentCatalog.load(tool_reg.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    project = await workspace_svc.add_local(str(repo_dir))

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-slow"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")
    await providers_svc.record_verification(
        f"{prov.name}:fake-slow", tool_calling=True, streaming=True, detail=None
    )

    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        git=GitRunner(paths.bin),
    )
    return manager, workspace_svc, project.id, prov.name, repo_dir


async def _collect_until_turn_ended(
    sub: Any,
    timeout: float = 5.0,  # noqa: ASYNC109
) -> list[Any]:
    events: list[Any] = []

    async def collector() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def test_stop_during_fake_slow(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)

    await manager.send(project_id, "orchestrator", "Slow turn 1")
    await manager.send(project_id, "orchestrator", "Quick turn 2")

    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    await manager.stop(project_id, "orchestrator")

    all_events: list[StreamEvent] = []
    ended: list[TurnEnded] = []
    async for ev in sub:
        all_events.append(ev)
        if isinstance(ev, TurnEnded):
            ended.append(ev)
            if len(ended) == 2:
                break

    assert ended[0].data.status == "stopped"
    assert ended[1].data.status == "done"
    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in all_events
    )

    store = manager._store  # pyright: ignore[reportPrivateUsage]
    sid = await store.session_id(project_id, "orchestrator")
    msgs = await store.messages(project_id, sid)
    agent_msg_1 = next(m for m in msgs if m.turn_id == ended[0].data.turn_id)
    assert agent_msg_1.status == "stopped"
    assert len(agent_msg_1.parts) > 0

    await manager.close()


class SlowDummyArgs(BaseModel):
    delay: float = 2.0


class SlowTool(AgentTool[SlowDummyArgs]):
    name = "slow_sleep"
    description = "Sleeps for a while."
    risk = RiskClass.READ
    args_model = SlowDummyArgs

    async def run(self, ctx: ToolContext, args: SlowDummyArgs) -> ToolOutput:
        await asyncio.sleep(args.delay)
        return ToolOutput(content="done")


async def test_stop_during_slow_tool(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    tools = default_registry()
    tools.register(SlowTool())
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, tools=tools
    )

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Call slow sleep")

    async for ev in sub:
        if isinstance(ev, ToolStarted):
            break

    await manager.stop(project_id, "orchestrator")

    stop_events: list[StreamEvent] = []
    async for ev in sub:
        stop_events.append(ev)
        if isinstance(ev, TurnEnded):
            assert ev.data.status == "stopped"
            break

    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in stop_events
    )

    m = await manager.send(project_id, "orchestrator", "New turn")
    assert m.status == "complete"
    evs = await _collect_until_turn_ended(sub)
    assert isinstance(evs[-1], TurnEnded)
    assert evs[-1].data.status == "done"

    await manager.close()
