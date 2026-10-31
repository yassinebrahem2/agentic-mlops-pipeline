"""The services one running app shares, built and torn down by its lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
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
from mlops_agents.monitoring.dashboard import DashboardBuilder
from mlops_agents.monitoring.expectations.job import CheckExpectationsJob
from mlops_agents.monitoring.expectations.query import (
    ExpectationsQueryProvider,
    ResultsQueryProvider,
)
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.monitoring.hooks.ci_dispatch import CiDispatchRunner, GitHubCiDispatcher
from mlops_agents.monitoring.hooks.command_runner import CommandRunner
from mlops_agents.monitoring.hooks.handoff import HandoffHooks
from mlops_agents.monitoring.hooks.placeholders import TrackerPlaceholderResolver
from mlops_agents.monitoring.hooks.query import HooksQueryProvider
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.incidents.dispatch import DiagnosisDispatch
from mlops_agents.monitoring.incidents.follow_ups import FollowUpsJob
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.maintenance import (
    MaintenanceKind,
    MaintenanceWindows,
    WindowsQueryProvider,
)
from mlops_agents.monitoring.incidents.prioritization import PrioritizationWake
from mlops_agents.monitoring.incidents.query import IncidentsQueryProvider, SignalsQueryProvider
from mlops_agents.monitoring.incidents.reactor import Reactor
from mlops_agents.monitoring.incidents.routing import OnCallRouter
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.incidents.severity import RuleSeverity
from mlops_agents.monitoring.incidents.suspects import SuspectLinker
from mlops_agents.monitoring.query import QueryProviders
from mlops_agents.monitoring.results.collect_job import CollectResultsJob
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.monitoring.scheduler.sync_jobs import (
    AfterSyncJob,
    SyncPrsJob,
    SyncTrackingJob,
)
from mlops_agents.monitoring.verdicts.baseline import (
    BaselineIncidentObserver,
    BaselineResultsObserver,
    SqliteBaseline,
)
from mlops_agents.monitoring.verdicts.labels import LabelConfirmation
from mlops_agents.monitoring.verdicts.query import VerdictsQueryProvider
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from mlops_agents.monitoring.waker import Waker
from mlops_agents.notifications.channels import default_channels
from mlops_agents.notifications.contract import (
    Channel,
    MorningReports,
    Notifier,
    OnCallPeople,
)
from mlops_agents.notifications.escalation import EscalationJob
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.on_call import OnCallSeeder, SqliteOnCallPeople
from mlops_agents.notifications.report import MorningReportJob, SqliteMorningReports
from mlops_agents.notifications.retries import RetryJob
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.onboarding.connections_kind import ConnectionsKind
from mlops_agents.onboarding.handoff_kind import HandoffKind
from mlops_agents.onboarding.kickoff import Kickoff
from mlops_agents.paths import DataPaths
from mlops_agents.promotion.contract import EvaluationReader, Reports
from mlops_agents.promotion.evaluation import SqliteEvaluationReader
from mlops_agents.promotion.pr_closed import ClosedUnmergedObserver
from mlops_agents.promotion.reports import SqliteReports
from mlops_agents.promotion.surfaces import GithubProtectionSurfaces
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.observer import ProposalApprovalObserver
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.providers.embedders import EmbeddersService
from mlops_agents.providers.failover import Clock, KeyFailover, SystemClock
from mlops_agents.providers.models import Provider
from mlops_agents.providers.service import ProvidersService
from mlops_agents.sandbox.docker import CliDockerRunner, DockerRunner
from mlops_agents.sandbox.projects import DatabaseSandboxProjects
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.stats.service import StatsService
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.adapters import default_adapters
from mlops_agents.tracking.service import TrackingService
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.lifecycle import TrainingLifecycle
from mlops_agents.training.linking import RequestLinker
from mlops_agents.training.outcomes import FixOutcomeObserver
from mlops_agents.training.queries import FixesQueryProvider, TrainingRequestsQueryProvider
from mlops_agents.training.requests import SqliteTraining
from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


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
    hooks: HooksService
    incidents: IncidentsService
    maintenance_windows: MaintenanceWindows
    verdicts: SqliteVerdicts
    baseline: SqliteBaseline
    waker: Waker
    integration_settings: IntegrationSettings
    notifier: Notifier
    on_call: OnCallPeople
    notification_channels: Mapping[str, Channel]
    expectations: ExpectationsService
    fixes: SqliteFixes
    training: SqliteTraining
    dashboard: DashboardBuilder
    stats: StatsService
    morning_reports: MorningReports


async def _app_secrets(
    providers: ProvidersService,
    github: GitHubAuth,
    settings: Settings,
    integration_settings: IntegrationSettings | None = None,
) -> list[str]:
    secrets: list[str] = []
    if settings.password:
        secrets.append(settings.password)
    if settings.access_token:
        secrets.append(settings.access_token)
    if settings.master_key:
        secrets.append(settings.master_key)

    secrets.extend(await providers.secret_values())
    if integration_settings is not None:
        secrets.extend(await integration_settings.secret_values())

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
    integration_settings = IntegrationSettings(databases, keystore)
    await integration_settings.init_cache()

    async def secret_source() -> list[str]:
        return await _app_secrets(providers, github, settings, integration_settings)

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

    runner = docker_runner if docker_runner is not None else CliDockerRunner()
    sandbox_projects = DatabaseSandboxProjects(databases, workspace, connections)
    sandbox = SandboxService(
        runner,
        projects=sandbox_projects,
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
    on_call = SqliteOnCallPeople(databases)
    on_call_seeder = OnCallSeeder(on_call, handoffs, databases)
    handoffs.add_observer(on_call_seeder)
    notification_channels = default_channels(integration_settings, http)
    notifier = SqliteNotifier(
        databases=databases,
        settings=integration_settings,
        channels=notification_channels,
        events=events,
        clock=clock,
        on_call=on_call,
    )
    hooks = HooksService(
        databases=databases,
        secrets=project_secrets,
        events=events,
        clock=clock,
    )
    command_runner = CommandRunner(
        sandbox=sandbox,
        projects=sandbox_projects,
        docker_runner=runner,
        keystore=keystore,
    )
    hooks.add_runner(command_runner)
    ci_dispatcher = GitHubCiDispatcher(http=http, github=github, workspace=workspace, git=git)
    ci_runner = CiDispatchRunner(dispatcher=ci_dispatcher, github=github)
    hooks.add_runner(ci_runner)
    handoff_hooks = HandoffHooks(hooks, handoffs)
    handoffs.add_observer(handoff_hooks)
    proposal_kinds = ProposalKinds()
    proposals = ProposalsService(databases, project_secrets, proposal_kinds, events)

    query_providers = QueryProviders()
    query_providers.register(HooksQueryProvider(hooks))
    query_providers.register(SignalsQueryProvider(databases))
    query_providers.register(IncidentsQueryProvider(databases))

    notification_channels = default_channels(integration_settings, http)
    notifier = SqliteNotifier(
        databases=databases,
        settings=integration_settings,
        channels=notification_channels,
        events=events,
        clock=clock,
    )

    maintenance_windows = MaintenanceWindows(databases=databases, clock=clock)
    maintenance = maintenance_windows
    results = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        maintenance=maintenance,
    )
    severity = RuleSeverity(handoffs=handoffs)
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=severity,
        maintenance=maintenance,
        clock=clock,
        handoffs=handoffs,
    )
    severity.bind_incidents(incidents)
    results.bind_incidents(incidents)
    results.bind_maintenance(maintenance)
    verdicts = SqliteVerdicts(databases, incidents, results, events, clock)
    incidents.bind_verdicts(verdicts)
    query_providers.register(VerdictsQueryProvider(verdicts))
    # Registered before the startup `deliver_undelivered` below, so a result stored before a
    # restart still opens its incident.
    signals_from_results = SignalsFromResults(incidents, maintenance, results)
    results.add_observer(signals_from_results)
    # After SignalsFromResults, so an incident an agent fire opens in the same batch exists.
    baseline = SqliteBaseline(databases, results, incidents, events, clock)
    results.add_observer(BaselineResultsObserver(baseline))
    incidents.add_observer(BaselineIncidentObserver(baseline))

    tracking = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=project_secrets,
        events=events,
        adapters=default_adapters(http),
        github_token=github.token,
        handoffs=handoffs,
    )
    results.bind_tracking(tracking)
    stack.push_async_callback(tracking.close)
    evaluation_reader = SqliteEvaluationReader(tracking, tasks)
    reports = SqliteReports(databases, evaluation_reader, events)

    surfaces = GithubProtectionSurfaces(hooks, http, github, tasks)
    pr_sync = PrSyncObserver(tasks, github, http, events)
    stack.push_async_callback(pr_sync.close)
    closed_unmerged = ClosedUnmergedObserver(knowledge, http, github, tasks)
    pr_sync.add_observer(closed_unmerged)

    wakeups = WakeupService(databases, events=events)
    waker = Waker(databases, events, clock)
    diagnosis_dispatch = DiagnosisDispatch(incidents, waker)

    router = OnCallRouter(on_call=on_call, databases=databases)
    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=on_call,
        notifier=notifier,
        dispatch=diagnosis_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)
    prioritization_wake = PrioritizationWake(waker=waker, events=events)
    incidents.add_observer(prioritization_wake)

    fixes = SqliteFixes(
        databases,
        incidents,
        waker,
        events,
        clock,
        handoffs=handoffs,
        notifier=notifier,
        on_call=on_call,
    )
    outcome_observer = FixOutcomeObserver(fixes, incidents)
    incidents.add_observer(outcome_observer)
    training = SqliteTraining(databases, events, clock)
    resolver = TrackerPlaceholderResolver(tracking)
    training_lifecycle = TrainingLifecycle(training, fixes, resolver, clock, events)
    hooks.add_lifecycle(training_lifecycle)

    # One instance for the hook, table-detail route, and follow-ups: they share one cache lock.
    data_summaries = DataSummaries(connections, ProfileCache(paths))
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
        hooks=hooks,
        incidents=incidents,
        severity=severity,
        fixes=fixes,
        query_providers=query_providers,
        data_summaries=data_summaries,
        connections=connections,
        clock=clock,
        surfaces=surfaces,
        pr_sync=pr_sync,
    )
    # The one hook registry. Each plan registers its hooks here, before the catalog loads; the
    # status board needs the catalog, so its hook is bound once the board exists.
    turn_hooks = HookRegistry()
    status_board_hook = StatusBoardHook()
    turn_hooks.register(status_board_hook)
    turn_hooks.register(KnowledgeHook(knowledge))
    turn_hooks.register(DataSummaryHook(connections, data_summaries))
    turn_hooks.register(TrackingSummaryHook(tracking))
    catalog = await asyncio.to_thread(
        AgentCatalog.load, tools.names(), hook_names=turn_hooks.names()
    )
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
        hooks=turn_hooks,
        audit_log=audit,
        event_log=events,
    )
    inbound.bind(sessions)
    status_board.bind_sessions(sessions)
    wakeups.bind(sessions)
    waker.bind(sessions)
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
    proposal_kinds.register(MaintenanceKind(windows=maintenance_windows))

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

    scheduler = AppScheduler(
        databases=databases,
        projects=_list_project_ids,
        clock=clock,
        sleep=clock.sleep,
    )
    scheduler.register(SyncTrackingJob(tracking))
    scheduler.register(SyncPrsJob(databases, pr_sync))
    scheduler.register(CollectResultsJob(databases, results))
    scheduler.register(AfterSyncJob(databases, scheduler, clock=clock))
    scheduler.add_sync_observer(LabelConfirmation(verdicts))

    retry_notifications_job = RetryJob(databases, notifier, clock)
    scheduler.register(retry_notifications_job)
    follow_ups_job = FollowUpsJob(
        databases=databases,
        waker=waker,
        handoffs=handoffs,
        data_summaries=data_summaries,
        connections=connections,
        clock=clock,
    )
    scheduler.register(follow_ups_job)

    suspect_linker = SuspectLinker(
        databases=databases,
        incidents=incidents,
        tracking=tracking,
        events=events,
        clock=clock,
    )
    scheduler.add_sync_observer(suspect_linker)
    pr_sync.add_observer(suspect_linker)

    expectations = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        tracking=tracking,
        maintenance=maintenance,
        clock=clock,
    )
    incidents.bind_expectations(expectations)
    handoffs.add_observer(expectations.handoff_observer)
    results.add_observer(expectations.results_observer)
    scheduler.register(CheckExpectationsJob(databases, expectations, clock=clock))
    query_providers.register(ResultsQueryProvider(results))
    query_providers.register(ExpectationsQueryProvider(expectations))
    query_providers.register(FixesQueryProvider(fixes))
    query_providers.register(TrainingRequestsQueryProvider(training))
    query_providers.register(WindowsQueryProvider(windows=maintenance_windows))
    training_lifecycle.bind_expectations(expectations)
    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,
        expectations=expectations,
        events=events,
        waker=waker,
    )
    scheduler.add_sync_observer(linker)
    stats = StatsService(
        databases=databases,
        incidents=incidents,
        verdicts=verdicts,
        baseline=baseline,
        results=results,
        hooks=hooks,
        models=providers,
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

    # Resume pending wakes before starting the scheduler, so a wake fired by a job that has
    # inserted its row but not posted yet cannot be posted twice. Results stored but not
    # delivered before a restart are delivered here too, once every results observer is
    # registered and before the scheduler's first collection.
    startup_now = clock.now()
    for project in await workspace.list_projects():
        await waker.resume(project.id)
        await results.deliver_undelivered(project.id, startup_now)

    # Start the scheduler after kickoff.resume(), and push scheduler.stop right after scheduler.start()
    # so it stops first on close (LIFO) and a failure earlier in startup can't leak its task.
    notification_channels = default_channels(integration_settings, http)
    notifier = SqliteNotifier(
        databases=databases,
        settings=integration_settings,
        channels=notification_channels,
        events=events,
        clock=clock,
        run_mode=settings.mode,
        on_call=on_call,
    )
    notifier.bind_incidents(incidents)
    retry_notifications_job = RetryJob(databases, notifier, clock)
    scheduler.register(retry_notifications_job)
    escalation_job = EscalationJob(
        databases=databases,
        incidents=incidents,
        on_call=on_call,
        notifier=notifier,
        clock=clock,
    )
    scheduler.register(escalation_job)
    morning_reports = SqliteMorningReports(
        databases=databases,
        incidents=incidents,
        hooks=hooks,
        approvals=approvals,
        expectations=expectations,
        events=events,
    )
    morning_report_job = MorningReportJob(
        databases=databases,
        on_call=on_call,
        notifier=notifier,
        reports=morning_reports,
        clock=clock,
        incidents=incidents,
        settings=integration_settings,
    )
    scheduler.register(morning_report_job)

    await scheduler.start()
    stack.push_async_callback(scheduler.stop)

    # Reconcile on-call and expectations across existing projects
    for project in await workspace.list_projects():
        try:
            await on_call_seeder.reconcile(project.id)
        except Exception:
            logger.exception("Failed to reconcile on-call for project %s", project.id)
        try:
            await expectations.reconcile(project.id)
        except Exception:
            logger.exception("Failed to reconcile expectations for project %s", project.id)

    dashboard = DashboardBuilder(
        handoffs=handoffs,
        tracking=tracking,
        incidents=incidents,
        expectations=expectations,
        results=results,
        baseline=baseline,
        status_board=status_board,
        mode=settings.mode,
    )

    # Reconcile handoff hooks across existing projects
    await handoff_hooks.reconcile([p.id for p in await workspace.list_projects()])

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
        hooks=hooks,
        incidents=incidents,
        maintenance_windows=maintenance_windows,
        verdicts=verdicts,
        baseline=baseline,
        waker=waker,
        integration_settings=integration_settings,
        notifier=notifier,
        on_call=on_call,
        notification_channels=notification_channels,
        expectations=expectations,
        fixes=fixes,
        training=training,
        dashboard=dashboard,
        stats=stats,
        morning_reports=morning_reports,
    )
