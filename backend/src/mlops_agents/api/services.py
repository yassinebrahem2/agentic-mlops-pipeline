"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2

from mlops_agents.agents.approvals.contract import Approvals
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.hooks.status_board import StatusBoardHook
from mlops_agents.agents.messaging.bus import SqliteMessageBus
from mlops_agents.agents.messaging.inbound import SqliteInboundSource
from mlops_agents.agents.messaging.settings import MessagingSettings
from mlops_agents.agents.messaging.wakeups import WakeupService
from mlops_agents.agents.pr_reminder import PrReminder
from mlops_agents.agents.pr_sync import PrSyncObserver
from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import ListAgentsTool, default_registry
from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.config import Settings
from mlops_agents.connections.contract import Connections
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.credentials import KeyStore, open_keystore
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.errors import DomainError
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import StatusBoard
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.observer import EventTurnObserver
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GhCli, GhLike, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.providers.embedders import EmbeddersService
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import Provider
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
    project_secrets: ProjectSecrets
    providers: ProvidersService
    github: GitHubAuth
    workspace: WorkspaceService
    catalog: AgentCatalog
    tools: ToolRegistry
    agent_settings: AgentSettingsService
    sessions: AgentSessions
    messaging_settings: MessagingSettings
    connections: Connections
    sandbox: SandboxService
    events: SqliteEventLog
    audit: SqliteAuditLog
    status_board: StatusBoard
    tasks: Tasks
    approvals: Approvals
    wakeups: WakeupService
    failover: KeyFailover
    embedders: EmbeddersService
    proposals: ProposalsService
    proposal_kinds: ProposalKinds
    handoffs: Handoffs


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
    # The one instance: every consumer of project secrets gets it through its constructor.
    project_secrets = ProjectSecrets(keystore, databases)
    workspace = WorkspaceService(settings, paths, databases, git, github, http, project_secrets)

    # The one key failover: every model and embedder call shares its key states. It is built
    # before the providers service that holds it, so its success hook looks the service up
    # when it runs.
    async def clear_attention(provider: Provider) -> None:
        await providers.clear_attention(provider.id)

    failover = KeyFailover(on_success=clear_attention)
    providers = ProvidersService(databases, keystore, http, failover)
    embedders = EmbeddersService(databases, providers, failover, http)

    async def secret_source() -> list[str]:
        return await _app_secrets(providers, github, settings)

    events = SqliteEventLog(databases)
    audit = SqliteAuditLog(databases, secrets=secret_source)
    messaging_settings = MessagingSettings(databases)
    bus = SqliteMessageBus(databases, events, messaging_settings)
    # The one approval store: it holds the approval observers, so every transition reaches them.
    approval_store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=approval_store, audit_log=audit, event_log=events)

    kind_registry = KindRegistry([LocalFolderKind(paths.root)])
    connections = ConnectionsService(databases, events, kind_registry)

    sandbox = SandboxService(
        docker_runner,
        projects=DatabaseSandboxProjects(databases, workspace, connections),
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

    wakeups = WakeupService(databases, events=events)
    tools = default_registry(
        tasks=tasks,
        sandbox=sandbox,
        bus=bus,
        wakeups=wakeups,
        workspace=workspace,
        github=github,
        events=events,
        http=http,
        databases=databases,
    )
    # The one hook registry. Each plan registers its hooks here, before the catalog loads; the
    # status board needs the catalog, so its hook is bound once the board exists.
    hooks = HookRegistry()
    status_board_hook = StatusBoardHook()
    hooks.register(status_board_hook)
    catalog = await asyncio.to_thread(AgentCatalog.load, tools.names(), hook_names=hooks.names())
    bus.use_catalog(catalog)
    wakeups.use_catalog(catalog)
    agent_settings = AgentSettingsService(databases, providers, catalog)
    status_board = SqliteStatusBoard(catalog, events)
    status_board_hook.bind(status_board)
    list_agents = tools.get("ListAgents")
    if isinstance(list_agents, ListAgentsTool):
        list_agents.bind(catalog, status_board)
    turn_observer = EventTurnObserver(events)
    inbound = SqliteInboundSource(databases)
    pr_sync = PrSyncObserver(tasks, github, http, events)
    stack.push_async_callback(pr_sync.close)

    sessions = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        providers=providers,
        http=http,
        failover=failover,
        gate=gate,
        inbound=inbound,
        turn_end=PrReminder(tasks),
        workspaces=tasks,
        approval_store=approval_store,
        observers=[turn_observer, inbound, pr_sync],
        bus=bus,
        hooks=hooks,
        audit_log=audit,
        event_log=events,
    )
    inbound.bind(sessions)
    status_board.bind_sessions(sessions)
    wakeups.bind(sessions)
    stack.push_async_callback(sessions.close)

    approvals = ApprovalsService(
        databases=databases,
        sessions=sessions,
        store=approval_store,
        audit_log=audit,
        event_log=events,
    )
    sessions.bind_approvals(approvals)
    proposal_kinds = ProposalKinds()
    proposals = ProposalsService(databases, project_secrets, proposal_kinds, events)
    handoffs = SqliteHandoffs(databases)

    # Last, because starting fires the due wake-ups, which can recover projects and expire
    # approvals: every service and observer hook must exist before anything can fire a
    # transition. Later plans register their observers above this line. Close is registered
    # first so a failed start still stops the timer.
    stack.push_async_callback(wakeups.close)
    await wakeups.start()

    return Services(
        settings=settings,
        paths=paths,
        databases=databases,
        keystore=keystore,
        project_secrets=project_secrets,
        providers=providers,
        github=github,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        agent_settings=agent_settings,
        sessions=sessions,
        messaging_settings=messaging_settings,
        connections=connections,
        sandbox=sandbox,
        events=events,
        audit=audit,
        status_board=status_board,
        tasks=tasks,
        approvals=approvals,
        wakeups=wakeups,
        failover=failover,
        embedders=embedders,
        proposals=proposals,
        proposal_kinds=proposal_kinds,
        handoffs=handoffs,
    )
