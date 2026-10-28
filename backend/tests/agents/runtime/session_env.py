"""One real SessionManager stack against the fake model server, shared by the session tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.contract import MessageBus
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded
from mlops_agents.agents.runtime.hooks import (
    Gate,
    InboundSource,
    TurnObserver,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import ToolRegistry, default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.events.contract import AuditLog
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.hook_fakes import NoInbound
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import FakeClock


@dataclass(frozen=True)
class SessionEnv:
    manager: SessionManager
    settings: AgentSettingsService
    # A second store on the same databases: the data is shared, so it sees what the manager wrote.
    store: SessionStore
    project_id: str
    provider: str
    approval_store: ApprovalStore  # the one the manager (and the default gate) were given
    providers: ProvidersService  # the one the manager was given, on the same key failover


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


async def build_session_env(
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
    tools: ToolRegistry | None = None,
    observers: Sequence[TurnObserver] = (),
    bus: MessageBus | None = None,
    gate: Gate | None = None,
    inbound: InboundSource | None = None,
    audit_log: AuditLog | None = None,
    approval_store: ApprovalStore | None = None,
) -> SessionEnv:
    repo_files = files if files is not None else {"README.md": "Line 1 of readme\nLine 2\n"}
    repo_dir = make_repo(tmp_path / "repo", repo_files, git_env)

    tool_reg = tools if tools is not None else default_registry()
    catalog = AgentCatalog.load(tool_reg.names() | STAGE_2_TOOLS, extra_dirs=extra_dirs or ())
    failover = KeyFailover(FakeClock())
    providers_svc = ProvidersService(databases, keystore, model_http, failover)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    workspace_svc = WorkspaceService(
        _settings(paths),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )

    approvals = approval_store if approval_store is not None else ApprovalStore(databases)
    manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        failover=failover,
        gate=gate if gate is not None else ApprovalGate(databases, store=approvals),
        inbound=inbound if inbound is not None else NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin)),
        approval_store=approvals,
        observers=observers,
        bus=bus,
        audit_log=audit_log,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-slow"))
    await providers_svc.verify_model(f"{prov.name}:fake-slow")

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    return SessionEnv(
        manager=manager,
        settings=settings_svc,
        store=SessionStore(databases),
        project_id=project.id,
        provider=prov.name,
        approval_store=approvals,
        providers=providers_svc,
    )


async def collect_until_turn_ended(
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


async def opened_stream(
    env: SessionEnv, agent: str = "orchestrator"
) -> AsyncGenerator[StreamEvent, None]:
    """The session's live stream, subscribed before the turn so it sees every event."""
    sub = env.manager.subscribe(env.project_id, agent, after_seq=None)
    await anext(sub)  # the opening resync
    return sub


async def turn_ended_statuses(sub: AsyncGenerator[StreamEvent, None]) -> list[str]:
    """The turn.ended events of a stream the manager's close() has ended."""

    async def drain() -> list[StreamEvent]:
        return [event async for event in sub]

    events = await asyncio.wait_for(drain(), timeout=10)
    return [e.data.status for e in events if isinstance(e, TurnEnded)]


async def agent_message_statuses(env: SessionEnv, agent: str = "orchestrator") -> list[str]:
    """The statuses of the agent's messages, oldest first."""
    session_id = await env.store.session_id(env.project_id, agent)
    return [
        m.status
        for m in await env.store.messages(env.project_id, session_id)
        if m.author_kind == "agent"
    ]
