"""What the kickoff tests read back, and a restarted app over the same data folder."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any

import httpx2
from sqlalchemy import select

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.onboarding.briefs import KICKOFF_AGENTS
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import kickoff, messages, sessions
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.hook_fakes import NoInbound
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS, MemoryKeyStore, memory_project_secrets
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock

PROPOSAL = "prp_first"


def sample_payload(**overrides: Any) -> HandoffPayload:
    return HandoffPayload.model_validate(handoff(**overrides))


def marker(proposal_id: str) -> str:
    """The first line of a kickoff message, as the plan writes it."""
    return f"[handoff {proposal_id}]"


@dataclass(frozen=True)
class PostedMessage:
    id: str
    session_agent: str
    from_agent: str | None
    author_name: str
    exchange_id: str | None
    hop: int | None
    text: str


async def handoff_messages(databases: Databases, project_id: str) -> list[PostedMessage]:
    """Every message authored "Handoff", oldest first, with the agent whose session holds it."""
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(messages, sessions.c.agent.label("session_agent"))
                    .join(sessions, sessions.c.id == messages.c.session_id)
                    .where(messages.c.author_name == "Handoff")
                    .order_by(messages.c.seq)
                )
            )
            .mappings()
            .all()
        )
    return [
        PostedMessage(
            id=str(r["id"]),
            session_agent=str(r["session_agent"]),
            from_agent=r["from_agent"],
            author_name=str(r["author_name"]),
            exchange_id=r["exchange_id"],
            hop=r["hop"],
            text="".join(p["text"] for p in json.loads(r["parts"]) if p["type"] == "text"),
        )
        for r in rows
    ]


async def kickoff_rows(databases: Databases, project_id: str) -> dict[tuple[int, str], Any]:
    """(version, agent) -> the row's mapping."""
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(kickoff))).mappings().all()
    return {(int(r["handoff_version"]), str(r["agent"])): r for r in rows}


async def all_idle(sessions: AgentSessions, project_id: str) -> None:
    """Every kickoff agent's turn has ended, so its rows and messages are final. Polled, as
    `session_env.until_idle` is: a session has no idle event to wait on."""
    async with asyncio.timeout(10):
        for agent in KICKOFF_AGENTS:
            while True:
                if not (await sessions.get(project_id, agent)).busy:
                    break
                await asyncio.sleep(0.01)


@dataclass(frozen=True)
class Restarted:
    databases: Databases
    manager: SessionManager
    http: httpx2.AsyncClient

    async def close(self) -> None:
        await self.manager.close()
        await self.http.aclose()
        await self.databases.dispose()


async def restart(
    paths: DataPaths, keystore: MemoryKeyStore, model_http: httpx2.AsyncClient
) -> Restarted:
    """A new Databases and SessionManager over the same data folder, as after a restart: the
    provider and project rows the first run wrote are reused, nothing is added."""
    databases = Databases(paths)
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    failover = KeyFailover(FakeClock())
    providers = ProvidersService(databases, keystore, model_http, failover)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    settings = replace(
        load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)}),
        mode=RunMode.LOCAL,
    )
    workspace = WorkspaceService(
        settings,
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )
    approvals = ApprovalStore(databases)
    manager = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=AgentSettingsService(databases, providers, catalog),
        providers=providers,
        http=model_http,
        failover=failover,
        gate=ApprovalGate(databases, store=approvals),
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=FakeWorkspaces(workspace=workspace, git=GitRunner(paths.bin)),
        approval_store=approvals,
    )
    return Restarted(databases=databases, manager=manager, http=http)
