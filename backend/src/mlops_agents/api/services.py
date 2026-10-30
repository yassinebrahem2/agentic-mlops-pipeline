"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2

from mlops_agents.agents.approvals.authority import AuthorityRegistry
from mlops_agents.agents.approvals.contract import Approvals
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.data_summary import DataSummaryHook
from mlops_agents.agents.hooks.knowledge import KnowledgeHook
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.hooks.status_board import StatusBoardHook
from mlops_agents.agents.hooks.tracking_summary import TrackingSummaryHook
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
from mlops_agents.data.cache import ProfileCache
from mlops_agents.data.summaries import DataSummaries
from mlops_agents.errors import DomainError
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import StatusBoard
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.observer import EventTurnObserver
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GhCli, GhLike, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.knowledge.contract import Knowledge
from mlops_agents.knowledge.service import KnowledgeService
from mlops_agents.knowledge.vectors import SqliteVectorStore
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.query import QueryProviders
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.onboarding.connections_kind import ConnectionsKind
from mlops_agents.onboarding.handoff_kind import HandoffKind
from mlops_agents.onboarding.kickoff import Kickoff
from mlops_agents.paths import DataPaths
from mlops_agents.promotion.contract import EvaluationReader, Reports
from mlops_agents.promotion.evaluation import SqliteEvaluationReader
from mlops_agents.promotion.reports import SqliteReports
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.observer import ProposalApprovalObserver
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.providers.embedders import EmbeddersService
from mlops_agents.providers.failover import Clock, KeyFailover, SystemClock
from mlops_agents.providers.models import Provider
from mlops_agents.providers.service import ProvidersService
from mlops_agents.sandbox.docker import DockerRunner
from mlops_agents.sandbox.projects import DatabaseSandboxProjects
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.adapters import default_adapters
from mlops_agents.tracking.loop import TrackingLoop
from mlops_agents.tracking.service import TrackingService
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
    data_summaries: DataSummaries
    sandbox: SandboxService
    events: SqliteEventLog
    audit: SqliteAuditLog
    status_board: StatusBoard
    tasks: Tasks
    approvals: Approvals
    wakeups: WakeupService
    failover: KeyFailover
    embedders: EmbeddersService
    knowledge: Knowledge
    tracking: TrackingService
    tracking_loop: TrackingLoop
    proposals: ProposalsService
    proposal_kinds: ProposalKinds
    handoffs: Handoffs
    query_providers: QueryProviders
    authorities: AuthorityRegistry
    clock: Clock
    scheduler: AppScheduler
    results: ResultsService
    kickoff: Kickoff
    evaluation_reader: EvaluationReader
    reports: Reports


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
    clock: Clock | None = None,
) -> Services:
    """Each service registers its cleanup on `stack` where it is built; closing the stack
    undoes them in reverse order."""
    if clock is None:
        clock = SystemClock()

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

    failover = KeyFailover(clock=clock, on_success=clear_attention)
    providers = ProvidersService(databases, keystore, http, failover)
    embedders = EmbeddersService(databases, providers, failover, http)

    async def secret_source() -> list[str]:
        return await _app_secrets(providers, github, settings)

    events = SqliteEventLog(databases)
    audit = SqliteAuditLog(databases, secrets=secret_source)
    vectors = SqliteVectorStore(databases)
    knowledge = KnowledgeService(databases, embedders, vectors, events, secret_source)
    stack.push_async_callback(knowledge.close)
    messaging_settings = MessagingSettings(databases)
    bus = SqliteMessageBus(databases, events, messaging_settings)
    # The one approval store: it holds the approval observers, so every transition reaches them.
    approval_store = ApprovalStore(databases)
    authorities = AuthorityRegistry()
    gate = ApprovalGate(
        databases,
        store=approval_store,
        audit_log=audit,
        event_log=events,
        authorities=authorities,
    )

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

    handoffs = SqliteHandoffs(databases)
    proposal_kinds = ProposalKinds()
    proposals = ProposalsService(databases, project_secrets, proposal_kinds, events)

    query_providers = QueryProviders()
    maintenance = NeverSuspended()
    _ = maintenance

    tracking = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=project_secrets,
        events=events,
        adapters=default_adapters(http),
        github_token=github.token,
        handoffs=handoffs,
    )
    stack.push_async_callback(tracking.close)
    evaluation_reader = SqliteEvaluationReader(tracking, tasks)
    reports = SqliteReports(databases, evaluation_reader, events)

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
        knowledge=knowledge,
        proposals=proposals,
        proposal_kinds=proposal_kinds,
        tracking=tracking,
        handoffs=handoffs,
        reports=reports,
    )
    # The one hook registry. Each plan registers its hooks here, before the catalog loads; the
    # status board needs the catalog, so its hook is bound once the board exists.
    hooks = HookRegistry()
    status_board_hook = StatusBoardHook()
    hooks.register(status_board_hook)
    hooks.register(KnowledgeHook(knowledge))
    # One instance for the hook and the table-detail route: they share one cache lock.
    data_summaries = DataSummaries(connections, ProfileCache(paths))
    hooks.register(DataSummaryHook(connections, data_summaries))
    hooks.register(TrackingSummaryHook(tracking))
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
    kickoff = Kickoff(databases, sessions, handoffs)
    # Before recovery: it re-applies an approved proposal that has no result yet.
    proposal_kinds.register(ConnectionsKind(connections, secret_source))
    proposal_kinds.register(
        HandoffKind(
            databases=databases,
            connections=connections,
            tracking=tracking,
            handoffs=handoffs,
            sandbox=sandbox,
            events=events,
            kickoff=kickoff,
            secrets=project_secrets,
            secret_values=secret_source,
        )
    )

    approvals = ApprovalsService(
        databases=databases,
        sessions=sessions,
        store=approval_store,
        audit_log=audit,
        event_log=events,
    )
    sessions.bind_approvals(approvals)

    proposal_observer = ProposalApprovalObserver(proposals, approvals, workspace)
    approvals.add_observer(proposal_observer)

    for project in await workspace.list_projects():
        await proposals.recover(project.id)

    async def _list_project_ids() -> list[str]:
        return [p.id for p in await workspace.list_projects()]

    tracking_loop = TrackingLoop(
        tracking=tracking,
        projects=_list_project_ids,
    )
    stack.push_async_callback(tracking_loop.close)
    await tracking_loop.start()

    scheduler = AppScheduler(
        databases=databases,
        projects=_list_project_ids,
        clock=clock,
        sleep=clock.sleep,
    )

    results = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
    )

    # Last, because starting fires the due wake-ups, which can recover projects and expire
    # approvals: every service and observer hook must exist before anything can fire a
    # transition. Later plans register their observers on the store
    # (`approval_store.observers.add(...)`) above this line. Close is registered first so a
    # failed start still stops the timer.
    stack.push_async_callback(wakeups.close)
    await wakeups.start()
    # Like a due wake-up, a lost kickoff starts turns, so it is finished after everything else
    # exists, and before the app serves.
    await kickoff.resume()

    # Start the scheduler after kickoff.resume(), and push scheduler.stop right after scheduler.start()
    # so it stops first on close (LIFO) and a failure earlier in startup can't leak its task.
    await scheduler.start()
    stack.push_async_callback(scheduler.stop)

    # Deliver undelivered results across restarts, after all observers and services are registered and ready.
    startup_now = clock.now()
    for project in await workspace.list_projects():
        await results.deliver_undelivered(project.id, startup_now)

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
        data_summaries=data_summaries,
        sandbox=sandbox,
        events=events,
        audit=audit,
        status_board=status_board,
        tasks=tasks,
        approvals=approvals,
        wakeups=wakeups,
        failover=failover,
        embedders=embedders,
        knowledge=knowledge,
        tracking=tracking,
        tracking_loop=tracking_loop,
        proposals=proposals,
        proposal_kinds=proposal_kinds,
        handoffs=handoffs,
        query_providers=query_providers,
        authorities=authorities,
        clock=clock,
        scheduler=scheduler,
        results=results,
        kickoff=kickoff,
        evaluation_reader=evaluation_reader,
        reports=reports,
    )
