"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2
from sqlalchemy import select

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
from mlops_agents.storage.tables import provider_keys
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


_logger = logging.getLogger(__name__)


async def _app_secrets(
    databases: Databases,
    keystore: KeyStore,
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

    try:
        app_engine = await databases.app()
        async with app_engine.connect() as conn:
            refs = list(await conn.scalars(select(provider_keys.c.keystore_ref)))
        for ref in refs:
            val = await asyncio.to_thread(keystore.get, ref)
            if val:
                secrets.append(val)
    except Exception:
        _logger.debug("Failed to read provider keys for secrets source", exc_info=True)

    try:
        gh_token = await github.token()
        if gh_token:
            secrets.append(gh_token)
    except Exception:
        _logger.debug("Failed to read GitHub token for secrets source", exc_info=True)

    return secrets


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

    async def secret_source() -> list[str]:
        return await _app_secrets(databases, keystore, github, settings)

    events = SqliteEventLog(databases)
    audit = SqliteAuditLog(databases, secrets=secret_source)

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
