"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.agents.runtime.hooks import (
    AllowAllGate,
    NoInbound,
    NoTurnEndCheck,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.config import Settings
from mlops_agents.credentials import KeyStore, open_keystore
from mlops_agents.errors import DomainError
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import StatusBoard
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.observer import EventTurnObserver
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GhCli, GhLike, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.service import ProvidersService
from mlops_agents.sandbox.docker import DockerRunner
from mlops_agents.sandbox.projects import DatabaseSandboxProjects
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService


@dataclass
class Services:
    settings: Settings
    paths: DataPaths
    databases: Databases
    keystore: KeyStore
    providers: ProvidersService
    github: GitHubAuth
    workspace: WorkspaceService
    catalog: AgentCatalog
    tools: ToolRegistry
    agent_settings: AgentSettingsService
    sessions: AgentSessions
    sandbox: SandboxService
    events: SqliteEventLog
    audit: SqliteAuditLog
    status_board: StatusBoard
    tasks: Tasks


async def _app_secrets(
    providers: ProvidersService,
    github: GitHubAuth,
    settings: Settings,
) -> list[str]:
    secrets: list[str] = []
    if settings.password:
        secrets.append(settings.password)
    if settings.access_token:
        secrets.append(settings.access_token)
    if settings.master_key:
        secrets.append(settings.master_key)

    secrets.extend(await providers.secret_values())

    try:
        gh_token = await github.token()
        if gh_token:
            secrets.append(gh_token)
    except DomainError as err:
        if err.code not in ("gh_not_logged_in", "gh_not_installed"):
            raise

    return secrets


async def build_services(
    stack: AsyncExitStack,
    settings: Settings,
    *,
    keystore: KeyStore | None,
    gh: GhLike | None,
    docker_runner: DockerRunner | None = None,
) -> Services:
    """Each service registers its cleanup on `stack` where it is built; closing the stack
    undoes them in reverse order."""
    paths = DataPaths(settings.data_dir)
    await asyncio.to_thread(paths.ensure)

    databases = Databases(paths)
    stack.push_async_callback(databases.dispose)
    # Migrate now, so a broken app.db stops startup instead of the first request.
    await databases.app()

    if keystore is None:
        keystore = await asyncio.to_thread(open_keystore, settings, paths)

    http = httpx2.AsyncClient()
    stack.push_async_callback(http.aclose)

    github = GitHubAuth(databases, keystore, http, gh if gh is not None else GhCli())
    git = GitRunner(paths.bin, token_provider=github.token)
    workspace = WorkspaceService(settings, paths, databases, git, github, http)
    providers = ProvidersService(databases, keystore, http)

    async def secret_source() -> list[str]:
        return await _app_secrets(providers, github, settings)

    events = SqliteEventLog(databases)
    audit = SqliteAuditLog(databases, secrets=secret_source)

    sandbox = SandboxService(
        docker_runner,
        projects=DatabaseSandboxProjects(databases, workspace),
        events=events,
    )
    await sandbox.cleanup_orphans()
    stack.push_async_callback(sandbox.close)

    tasks = TasksService(
        databases=databases,
        paths=paths,
        git=git,
        events=events,
        workspace=workspace,
        sandbox=sandbox,
    )

    tools = default_registry(tasks=tasks, sandbox=sandbox)
    catalog = await asyncio.to_thread(AgentCatalog.load, tools.names())
    agent_settings = AgentSettingsService(databases, providers, catalog)

    status_board = SqliteStatusBoard(catalog, events)
    turn_observer = EventTurnObserver(events)

    sessions = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        providers=providers,
        http=http,
        gate=AllowAllGate(),
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=tasks,
        observers=[turn_observer],
        status_board=status_board,
    )
    status_board.bind_sessions(sessions)
    stack.push_async_callback(sessions.close)

    return Services(
        settings=settings,
        paths=paths,
        databases=databases,
        keystore=keystore,
        providers=providers,
        github=github,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        agent_settings=agent_settings,
        sessions=sessions,
        sandbox=sandbox,
        events=events,
        audit=audit,
        status_board=status_board,
        tasks=tasks,
    )
