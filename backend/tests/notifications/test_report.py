"""Tests for MorningReports and MorningReportJob (Task 5, S4 §8, §13, §14)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.contract import Approvals, ApprovalView
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import (
    ExpectationKey,
    Expectations,
    ExpectationState,
    NeverSuspended,
)
from mlops_agents.monitoring.hooks.contract import HookRunView, Hooks
from mlops_agents.monitoring.incidents.contract import (
    IncidentView,
    Severity,
    SeverityDecision,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.results.contract import (
    CollectionReport,
    ResultQuery,
    ResultsObserver,
    StoredResult,
)
from mlops_agents.notifications.channels import default_channels
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.on_call import SqliteOnCallPeople
from mlops_agents.notifications.report import (
    MorningReportJob,
    SqliteMorningReports,
    report_message,
)
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Contact, OnCall, OnCallHours
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log
from mlops_agents.tracking.contract import Page
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    async def sleep(self, seconds: float) -> None:
        pass


class MemoryKeyStore(KeyStore):
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.store.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.store[ref] = value

    def delete(self, ref: str) -> None:
        self.store.pop(ref, None)


class FakeResults:
    def __init__(self) -> None:
        self.observers: list[ResultsObserver] = []

    def add_observer(self, observer: ResultsObserver) -> None:
        self.observers.append(observer)

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]:
        return []

    async def ingest(
        self, project_id: str, source_id: str, items: Any, seen_at: datetime
    ) -> list[StoredResult]:
        return []

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        return CollectionReport(stored=0, by_source={}, failures={})

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        return Page(rows=[], total=0, truncated=False)


class FakeSeverityPolicy:
    def __init__(self) -> None:
        self.overrides: dict[str, Severity] = {}

    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision:
        sev = self.overrides.get(incident.id, incident.severity or "medium")
        return SeverityDecision(severity=sev, reasons=["Test severity"])


class FakeHooks:
    def __init__(self, runs_list: list[HookRunView] | None = None) -> None:
        self._runs = runs_list or []

    async def runs(
        self,
        project_id: str,
        *,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[HookRunView]:
        res = self._runs
        if since is not None:
            since_utc = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
            res = [r for r in res if r.started >= since_utc]
        if kinds is not None:
            res = [r for r in res if r.kind in kinds]
        return res[:limit]


class FakeApprovals:
    def __init__(self, pending_list: list[ApprovalView] | None = None) -> None:
        self._pending = pending_list or []

    async def pending(self, project_id: str) -> list[ApprovalView]:
        return list(self._pending)


class FakeExpectations:
    def __init__(self, states_list: list[ExpectationState] | None = None) -> None:
        self._states = states_list or []

    async def states(self, project_id: str) -> list[ExpectationState]:
        return list(self._states)


@pytest.fixture
def fake_discord() -> Iterator[RunningFakeDiscord]:
    state = FakeDiscordState()
    server = start_fake_discord(state)
    try:
        yield server
    finally:
        server.stop()


async def setup_report_env(
    tmp_path: Path,
    fake_discord: RunningFakeDiscord,
    start_time: datetime | None = None,
) -> dict[str, Any]:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    settings = IntegrationSettings(databases, keystore)
    events = SqliteEventLog(databases)
    clock = FakeClock(start_time or datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC))

    webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_secret"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    http = httpx2.AsyncClient()
    channels = default_channels(settings, http)
    notifier = SqliteNotifier(
        databases=databases,
        settings=settings,
        channels=channels,
        events=events,
        clock=clock,
    )

    results = FakeResults()
    severity = FakeSeverityPolicy()
    maintenance = NeverSuspended()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=severity,
        maintenance=maintenance,
        clock=clock,
    )
    notifier.bind_incidents(incidents)

    on_call = SqliteOnCallPeople(databases)
    hooks = FakeHooks()
    approvals = FakeApprovals()
    expectations = FakeExpectations()

    reports = SqliteMorningReports(
        databases=databases,
        incidents=incidents,
        hooks=cast(Hooks, hooks),
        approvals=cast(Approvals, approvals),
        expectations=cast(Expectations, expectations),
        events=events,
    )

    job = MorningReportJob(
        databases=databases,
        on_call=on_call,
        notifier=notifier,
        reports=reports,
        clock=clock,
        incidents=incidents,
        settings=settings,
    )

    return {
        "paths": paths,
        "databases": databases,
        "keystore": keystore,
        "settings": settings,
        "events": events,
        "clock": clock,
        "notifier": notifier,
        "incidents": incidents,
        "on_call": on_call,
        "hooks": hooks,
        "approvals": approvals,
        "expectations": expectations,
        "reports": reports,
        "job": job,
        "severity": severity,
        "http": http,
    }


@pytest.mark.asyncio
async def test_night_morning_report_full(tmp_path: Path, fake_discord: RunningFakeDiscord) -> None:
    """Full simulated night: critical, high outside hours, rollback, hold, pending approval,
    suspended expectation, offline resolution, skipped wake. Person's report lists each once."""
    start_time = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    # Set up on-call person: Alice, hours 09:00 - 17:00 UTC
    person_alice = OnCall(
        name="Alice Admin",
        contacts=[Contact(channel="discord", address="111222333")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=1,
    )
    people = await env["on_call"].replace(project_id, [person_alice])
    alice = people[0]

    clock: FakeClock = env["clock"]
    incidents: IncidentsService = env["incidents"]
    events: SqliteEventLog = env["events"]

    # 1. Critical incident at 02:00 UTC
    clock.advance(timedelta(hours=2))  # 02:00 UTC
    crit_draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="null_check",
        ran_at=clock.now(),
        detail={"error": "critical drift in null values"},
    )
    outcome_crit = await incidents.raise_signal(project_id, crit_draft, seen_at=clock.now())
    crit_inc_id = outcome_crit.incident_id
    await incidents.set_severity(project_id, crit_inc_id, "critical", "Critical data issue", "test")

    # 2. High incident outside hours at 04:00 UTC
    clock.advance(timedelta(hours=2))  # 04:00 UTC
    high_draft = SignalDraft(
        kind="fired_result",
        category="model_performance",
        subject="auc_check",
        ran_at=clock.now(),
        detail={"error": "AUC dropped below threshold"},
    )
    outcome_high = await incidents.raise_signal(project_id, high_draft, seen_at=clock.now())
    high_inc_id = outcome_high.incident_id
    await incidents.set_severity(project_id, high_inc_id, "high", "High performance drop", "test")

    # 3. Incident resolved offline at 05:00 UTC
    clock.advance(timedelta(hours=1))  # 05:00 UTC
    offline_draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="disk_check",
        ran_at=clock.now(),
        cleared=True,  # resolved_offline = True
        detail={"error": "disk space resolved"},
    )
    outcome_offline = await incidents.raise_signal(project_id, offline_draft, seen_at=clock.now())
    offline_inc_id = outcome_offline.incident_id

    # 4. Rule-backed rollback (Hooks.runs row) at 03:00 UTC
    hook_run = HookRunView(
        id="hr_1",
        hook_id="rollback_hook",
        kind="rollback",
        incident_id=crit_inc_id,
        reason="model drift surpassed rule threshold",
        basis="rule",
        basis_ref="rule_drift_rollback",
        started=datetime(2026, 10, 30, 3, 0, 0, tzinfo=UTC),
    )
    env["hooks"]._runs.append(hook_run)

    # 5. Hold recorded event at 03:30 UTC
    await events.record(
        project_id,
        source="system",
        type="incident.hold_recorded",
        subject_id=crit_inc_id,
        payload={
            "incident_id": crit_inc_id,
            "reason": "safety hold on production pipeline",
        },
    )

    # 6. Pending approval
    pending_app = ApprovalView(
        id="app_1",
        agent="model_performance",
        session_id="sess_1",
        turn_id="turn_1",
        kind="tool",
        tool_call_id="call_1",
        tool="retrain_candidate",
        risk_class=RiskClass.DEPLOY,
        step=1,
        proposal_kind="retrain",
        preview=Preview(kind="text", title="Retrain", body="Retrain candidate model"),
        status="pending",
        note=None,
        created_at="2026-10-30T04:30:00Z",
        decided_at=None,
    )
    env["approvals"]._pending.append(pending_app)

    # 7. Suspended expectation
    susp_key = ExpectationKey(kind="job", ref="nightly_batch_eval", model=None)
    env["expectations"]._states.append(
        ExpectationState(
            key=susp_key,
            category="model_performance",
            active=True,
            status="suspended",
        )
    )

    # 8. Skipped wake event at 06:00 UTC
    await events.record(
        project_id,
        source="system",
        type="incident.wake_skipped",
        subject_id=crit_inc_id,
        payload={"agent": "data_quality", "label": "hourly_check", "key": "k1"},
    )

    # 9. Advance clock to 09:00:00 UTC (start of Alice's shift)
    clock.advance(timedelta(hours=4))  # now 09:00 UTC
    now_utc = clock.now()

    # Verify due()
    job: MorningReportJob = env["job"]
    dues = await job.due(project_id, after=start_time, now=now_utc)
    assert len(dues) == 1
    assert dues[0].kind == JobKind.MORNING_REPORT
    assert dues[0].due == datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)
    assert dues[0].subject == alice.id

    # Verify MorningReports.build()
    reports: SqliteMorningReports = env["reports"]
    body = await reports.build(project_id, alice.id, since=start_time)

    assert body.person == alice.id
    assert crit_inc_id in body.incidents
    assert high_inc_id in body.incidents
    assert offline_inc_id in body.incidents
    assert body.resolved_offline == [offline_inc_id]

    # Actions: rollback (basis rule) and hold (basis tier1)
    assert len(body.actions) == 2
    rb_action = next(a for a in body.actions if a.action == "rollback")
    assert rb_action.basis == "rule"
    assert "drift" in rb_action.why
    assert rb_action.basis_ref == "rule_drift_rollback"

    hold_action = next(a for a in body.actions if a.action == "hold")
    assert hold_action.basis == "tier1"
    assert "safety hold" in hold_action.why

    # Waiting: one line per pending approval
    assert len(body.waiting) == 1
    assert "model_performance: retrain" in body.waiting[0]

    # Suspended: nightly_batch_eval
    assert len(body.suspended) == 1
    assert body.suspended[0].ref == "nightly_batch_eval"

    # Wakes skipped
    assert len(body.wakes_skipped) == 1
    assert crit_inc_id in body.wakes_skipped[0]

    # Verify report_message
    incidents_map = {
        crit_inc_id: await incidents.get(project_id, crit_inc_id),
        high_inc_id: await incidents.get(project_id, high_inc_id),
        offline_inc_id: await incidents.get(project_id, offline_inc_id),
    }
    # Clean map (filter None)
    clean_map = {k: v for k, v in incidents_map.items() if v is not None}
    msg = report_message(body, clean_map)
    assert msg.severity == "critical"
    assert msg.urgent is False

    # Execute job.run()
    await job.run(project_id, dues[0])

    # Check fake discord delivered messages
    delivered = fake_discord.state.posts
    assert len(delivered) == 1
    content = delivered[0]["content"]

    # The person's report lists each once, with the action's basis
    assert crit_inc_id in content
    assert high_inc_id in content
    assert offline_inc_id in content
    assert "rollback" in content
    assert "rule" in content
    assert "hold" in content
    assert "tier1" in content
    assert "model_performance: retrain" in content
    assert "nightly_batch_eval" in content
    assert "wake skipped" in content

    # Check notification_log row
    engine = await env["databases"].project(project_id)
    async with engine.connect() as conn:
        stmt = select(notification_log).where(
            notification_log.c.person == alice.id,
            notification_log.c.kind == "report",
        )
        rows = (await conn.execute(stmt)).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"

    # Check report.sent event
    evt_page = await events.page(project_id, types=["report.sent"])
    assert len(evt_page) == 1


@pytest.mark.asyncio
async def test_re_run_same_due_sends_nothing(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    """A second run for the same due sends nothing (rows are the record)."""
    start_time = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    person = OnCall(
        name="Bob Worker",
        contacts=[Contact(channel="discord", address="222333444")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=1,
    )
    people = await env["on_call"].replace(project_id, [person])
    bob = people[0]

    clock: FakeClock = env["clock"]
    incidents: IncidentsService = env["incidents"]

    # Raise an incident at 03:00 UTC
    clock.advance(timedelta(hours=3))
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="schema_check",
        ran_at=clock.now(),
        detail={"error": "column missing"},
    )
    await incidents.raise_signal(project_id, draft, seen_at=clock.now())

    clock.advance(timedelta(hours=6))  # now 09:00 UTC
    due_time = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)
    job_due = JobDue(kind=JobKind.MORNING_REPORT, due=due_time, subject=bob.id)

    job: MorningReportJob = env["job"]

    # First run
    await job.run(project_id, job_due)
    assert len(fake_discord.state.posts) == 1

    # Second run with identical due
    await job.run(project_id, job_due)
    # Still 1 message, no second send
    assert len(fake_discord.state.posts) == 1


@pytest.mark.asyncio
async def test_reports_follow_dst_change_paris(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    """Reports follow the person's time zone across a DST change (Europe/Paris fallback in October)."""
    # 2026-10-24: Europe/Paris is UTC+2 (09:00 local = 07:00 UTC)
    # 2026-10-25: Daylight saving ends at 03:00 -> 02:00, becomes UTC+1 (09:00 local = 08:00 UTC)
    start_time = datetime(2026, 10, 24, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    person = OnCall(
        name="Claire Paris",
        contacts=[Contact(channel="discord", address="333444555")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="Europe/Paris",
        escalation_order=1,
    )
    people = await env["on_call"].replace(project_id, [person])
    claire = people[0]

    job: MorningReportJob = env["job"]

    # Day 1: 2026-10-24 07:05 UTC (09:05 Paris, UTC+2)
    now_day1 = datetime(2026, 10, 24, 7, 5, 0, tzinfo=UTC)
    dues_day1 = await job.due(project_id, after=start_time, now=now_day1)
    assert len(dues_day1) == 1
    assert dues_day1[0].due == datetime(2026, 10, 24, 7, 0, 0, tzinfo=UTC)

    # Simulate Day 1 report sent
    engine = await env["databases"].project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            notification_log.insert().values(
                id="log_d1",
                incident=None,
                person=claire.id,
                channel="discord",
                kind="report",
                escalation_step=0,
                status="sent",
                attempts=1,
                next_attempt=None,
                last_error=None,
                message="{}",
                created=datetime(2026, 10, 24, 7, 0, 0, tzinfo=UTC).isoformat(),
                sent=datetime(2026, 10, 24, 7, 0, 0, tzinfo=UTC).isoformat(),
            )
        )

    # Day 2: 2026-10-25 08:05 UTC (09:05 Paris, UTC+1 after fall-back)
    now_day2 = datetime(2026, 10, 25, 8, 5, 0, tzinfo=UTC)
    dues_day2 = await job.due(
        project_id, after=datetime(2026, 10, 24, 7, 0, 0, tzinfo=UTC), now=now_day2
    )
    assert len(dues_day2) == 1
    assert dues_day2[0].due == datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)

    # Day 3: 2026-10-26 08:05 UTC (09:05 Paris, UTC+1)
    now_day3 = datetime(2026, 10, 26, 8, 5, 0, tzinfo=UTC)
    dues_day3 = await job.due(
        project_id, after=datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC), now=now_day3
    )
    assert len(dues_day3) == 1
    assert dues_day3[0].due == datetime(2026, 10, 26, 8, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_two_people_different_hours(tmp_path: Path, fake_discord: RunningFakeDiscord) -> None:
    """Two people with different hours get their own report at their own time."""
    start_time = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    # Alice starts at 08:00 UTC, Bob starts at 10:00 UTC
    person_a = OnCall(
        name="Alice Eight",
        contacts=[Contact(channel="discord", address="888888")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="08:00",
            end="16:00",
        ),
        timezone="UTC",
        escalation_order=1,
    )
    person_b = OnCall(
        name="Bob Ten",
        contacts=[Contact(channel="discord", address="101010")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="10:00",
            end="18:00",
        ),
        timezone="UTC",
        escalation_order=2,
    )
    people = await env["on_call"].replace(project_id, [person_a, person_b])
    alice = people[0]
    bob = people[1]

    # Add an incident
    incidents: IncidentsService = env["incidents"]
    clock: FakeClock = env["clock"]
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="check_1",
        ran_at=clock.now(),
        detail={"err": "val"},
    )
    await incidents.raise_signal(project_id, draft, seen_at=clock.now())

    job: MorningReportJob = env["job"]

    # At 08:15 UTC, only Alice is due
    t_0815 = datetime(2026, 10, 30, 8, 15, 0, tzinfo=UTC)
    clock.current = t_0815
    dues_0815 = await job.due(project_id, after=start_time, now=t_0815)
    assert len(dues_0815) == 1
    assert dues_0815[0].subject == alice.id
    assert dues_0815[0].due == datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # Run Alice's report
    await job.run(project_id, dues_0815[0])
    assert len(fake_discord.state.posts) == 1

    # At 10:15 UTC, Bob is due, and Alice is not due again
    t_1015 = datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)
    clock.current = t_1015
    dues_1015 = await job.due(project_id, after=t_0815, now=t_1015)
    assert len(dues_1015) == 1
    assert dues_1015[0].subject == bob.id
    assert dues_1015[0].due == datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # Run Bob's report
    await job.run(project_id, dues_1015[0])
    assert len(fake_discord.state.posts) == 2


@pytest.mark.asyncio
async def test_empty_night_sends_nothing(tmp_path: Path, fake_discord: RunningFakeDiscord) -> None:
    """An empty night sends nothing."""
    start_time = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    person = OnCall(
        name="Dave Quiet",
        contacts=[Contact(channel="discord", address="999000")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=1,
    )
    people = await env["on_call"].replace(project_id, [person])
    dave = people[0]

    job: MorningReportJob = env["job"]
    due = JobDue(
        kind=JobKind.MORNING_REPORT,
        due=datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC),
        subject=dave.id,
    )

    # Run job on completely clean project with no night events
    await job.run(project_id, due)

    # No message sent
    assert len(fake_discord.state.posts) == 0

    # No notification_log row created
    engine = await env["databases"].project(project_id)
    async with engine.connect() as conn:
        stmt = select(notification_log).where(
            notification_log.c.person == dave.id,
            notification_log.c.kind == "report",
        )
        rows = (await conn.execute(stmt)).all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_downtime_catchup_latest_missed_only(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    """After downtime (multiple missed days), only the latest missed occurrence per person is emitted."""
    start_time = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)
    env = await setup_report_env(tmp_path, fake_discord, start_time)
    project_id = "prj_1"

    person = OnCall(
        name="Eve Downtime",
        contacts=[Contact(channel="discord", address="123987")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=1,
    )
    people = await env["on_call"].replace(project_id, [person])
    eve = people[0]

    # Last report was 2026-10-20 09:00 UTC
    engine = await env["databases"].project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            notification_log.insert().values(
                id="log_old",
                incident=None,
                person=eve.id,
                channel="discord",
                kind="report",
                escalation_step=0,
                status="sent",
                attempts=1,
                next_attempt=None,
                last_error=None,
                message="{}",
                created=datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC).isoformat(),
                sent=datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC).isoformat(),
            )
        )

    job: MorningReportJob = env["job"]

    # 3 days later: 2026-10-23 11:00 UTC. Missed Oct 21, Oct 22, Oct 23.
    now = datetime(2026, 10, 23, 11, 0, 0, tzinfo=UTC)
    dues = await job.due(project_id, after=datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC), now=now)

    # Only ONE due is emitted, and it is the latest missed occurrence (Oct 23 09:00 UTC)
    assert len(dues) == 1
    assert dues[0].due == datetime(2026, 10, 23, 9, 0, 0, tzinfo=UTC)
    assert dues[0].subject == eve.id
