"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.config import Settings
from mlops_agents.credentials import KeyStore, open_keystore
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.integrations.github.auth import GhCli, GhLike, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.service import ProvidersService
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
    events: SqliteEventLog
    audit: SqliteAuditLog


async def build_services(
    stack: AsyncExitStack, settings: Settings, *, keystore: KeyStore | None, gh: GhLike | None
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
    tools = default_registry()
    catalog = await asyncio.to_thread(AgentCatalog.load, tools.names())
    agent_settings = AgentSettingsService(databases, providers, catalog)
    sessions = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        providers=providers,
        http=http,
        git=git,
    )
    stack.push_async_callback(sessions.close)

    events = SqliteEventLog(databases)
    audit = SqliteAuditLog(databases)

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
        events=events,
        audit=audit,
    )
