"""Tests for EscalationJob and channel-failure signal (Task 4, S4 §7, §8, §13)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.incidents.contract import (
    IncidentView,
    Severity,
    SeverityDecision,
    SignalDraft,
    cause_key,
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
from mlops_agents.notifications.contract import Message, OnCallPerson
from mlops_agents.notifications.escalation import EscalationJob
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.on_call import SqliteOnCallPeople
from mlops_agents.notifications.retries import RetryJob
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Contact, OnCall, OnCallHours
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log
from mlops_agents.tracking.contract import Page
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
from tests.fakes.fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

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
    def __init__(self, default: Severity = "critical") -> None:
        self.default: Severity = default

    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision:
        return SeverityDecision(severity=self.default, reasons=["Test severity decision"])


@pytest.fixture
def fake_discord() -> Iterator[RunningFakeDiscord]:
    state = FakeDiscordState()
    server = start_fake_discord(state)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_smtp() -> Iterator[RunningFakeSmtp]:
    state = FakeSmtpState()
    server = start_fake_smtp(state)
    try:
        yield server
    finally:
        server.stop()


async def setup_test_env(
    tmp_path: Path,
    fake_discord: RunningFakeDiscord,
    fake_smtp: RunningFakeSmtp | None = None,
    default_severity: Severity = "critical",
) -> dict[str, Any]:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    settings = IntegrationSettings(databases, keystore)
    events = SqliteEventLog(databases)
    clock = FakeClock()

    webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_secret"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    if fake_smtp is not None:
        await settings.set_email(
            enabled=True,
            host=fake_smtp.host,
            port=fake_smtp.port,
            user="user@example.com",
            sender="noreply@example.com",
            starttls=False,
        )

    results = FakeResults()
    severity = FakeSeverityPolicy(default=default_severity)
    maintenance = NeverSuspended()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=severity,
        maintenance=maintenance,
        clock=clock,
    )

    on_call = SqliteOnCallPeople(databases)
    await on_call.replace(
        "prj_1",
        [
            OnCall(
                name="Alice",
                contacts=[Contact(channel="discord", address="u_alice")],
                hours=OnCallHours(
                    days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    start="00:00",
                    end="23:59",
                ),
                timezone="UTC",
                escalation_order=1,
            ),
            OnCall(
                name="Bob",
                contacts=[Contact(channel="discord", address="u_bob")],
                hours=OnCallHours(
                    days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    start="00:00",
                    end="23:59",
                ),
                timezone="UTC",
                escalation_order=2,
            ),
            OnCall(
                name="Charlie",
                contacts=[Contact(channel="discord", address="u_charlie")],
                hours=OnCallHours(
                    days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    start="00:00",
                    end="23:59",
                ),
                timezone="UTC",
                escalation_order=3,
            ),
        ],
    )

    return {
        "paths": paths,
        "databases": databases,
        "keystore": keystore,
        "settings": settings,
        "events": events,
        "clock": clock,
        "incidents": incidents,
        "on_call": on_call,
        "severity": severity,
    }


async def test_critical_incident_pages_second_person_then_third(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            notifier.bind_incidents(incidents)

            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            # Raise critical incident
            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="null_rate_check",
                    model=None,
                    ran_at=clock.now(),
                    detail={"issue": "null rate exceeded"},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            # Step 0 row (sent to Alice by reactor / step 0)
            people = await on_call.list("prj_1")
            alice = people[0]
            step0_msg = Message(
                title="Critical Data Quality Alert",
                severity="critical",
                cause=None,
                lines=["Null rate is 45%"],
                urgent=False,
            )
            await notifier.notify(
                "prj_1",
                alice,
                step0_msg,
                kind="incident",
                incident_id=incident_id,
            )

            # At T0 + 10m (timeout is 15m), not due yet
            clock.advance(timedelta(minutes=10))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=10), clock.now()
            )
            assert dues == []

            # Advance to T0 + 15m -> now escalation is due!
            clock.advance(timedelta(minutes=5))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=5), clock.now()
            )
            assert len(dues) == 1
            assert dues[0].kind == JobKind.ESCALATE
            assert dues[0].subject == incident_id

            # Run escalation: pages Bob (2nd person)
            await escalation_job.run("prj_1", dues[0])

            # Verify Bob received escalation message
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(notification_log).order_by(
                                notification_log.c.created.asc(), notification_log.c.id.asc()
                            )
                        )
                    )
                    .mappings()
                    .all()
                )

            assert len(rows) == 2
            assert rows[0]["person"] == alice.id
            assert rows[0]["kind"] == "incident"
            assert rows[0]["escalation_step"] == 0

            bob = people[1]
            assert rows[1]["person"] == bob.id
            assert rows[1]["kind"] == "escalation"
            assert rows[1]["escalation_step"] == 1
            assert rows[1]["status"] == "sent"

            bob_msg = Message.model_validate_json(rows[1]["message"])
            assert bob_msg.urgent is True
            assert bob_msg.diagnosis_pending is True  # cause was None

            # Advance another 15m -> pages Charlie (3rd person)
            clock.advance(timedelta(minutes=15))
            dues2 = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert len(dues2) == 1
            assert dues2[0].subject == incident_id

            await escalation_job.run("prj_1", dues2[0])

            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(notification_log).order_by(
                                notification_log.c.created.asc(), notification_log.c.id.asc()
                            )
                        )
                    )
                    .mappings()
                    .all()
                )

            assert len(rows) == 3
            charlie = people[2]
            assert rows[2]["person"] == charlie.id
            assert rows[2]["kind"] == "escalation"
            assert rows[2]["escalation_step"] == 2
            assert rows[2]["status"] == "sent"
    finally:
        await databases.dispose()


async def test_acknowledgement_before_timeout_stops_escalation(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="null_check",
                    ran_at=clock.now(),
                    detail={},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            people = await on_call.list("prj_1")
            msg = Message(title="Alert", severity="critical")
            await notifier.notify("prj_1", people[0], msg, kind="incident", incident_id=incident_id)

            # Acknowledge before timeout (e.g. at T0 + 8m)
            clock.advance(timedelta(minutes=8))
            await incidents.acknowledge("prj_1", incident_id, by="alice")

            # Advance past timeout (T0 + 15m)
            clock.advance(timedelta(minutes=10))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=10), clock.now()
            )
            assert dues == []

            # If run is called with a dummy job, it should do nothing
            dummy_due = JobDue(
                kind=JobKind.ESCALATE, due=clock.now() - timedelta(minutes=3), subject=incident_id
            )
            await escalation_job.run("prj_1", dummy_due)

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).all()
            # Only step 0 exists
            assert len(rows) == 1
    finally:
        await databases.dispose()


async def test_snoozed_incident_is_not_escalated(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="drift_check",
                    ran_at=clock.now(),
                    detail={},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            people = await on_call.list("prj_1")
            msg = Message(title="Alert", severity="critical")
            await notifier.notify("prj_1", people[0], msg, kind="incident", incident_id=incident_id)

            # Snooze for 40 minutes (at T0 + 5m until T0 + 45m)
            clock.advance(timedelta(minutes=5))
            snooze_until = clock.now() + timedelta(minutes=40)
            await incidents.snooze("prj_1", incident_id, until=snooze_until, by="alice")

            # At T0 + 20m (past initial 15m timeout, but during snooze)
            clock.advance(timedelta(minutes=15))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert dues == []

            # Advance past snooze (to T0 + 50m)
            clock.advance(timedelta(minutes=30))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=30), clock.now()
            )
            assert len(dues) == 1
            assert dues[0].subject == incident_id

            await escalation_job.run("prj_1", dues[0])
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).all()
            assert len(rows) == 2
            assert rows[1].kind == "escalation"
    finally:
        await databases.dispose()


async def test_rerun_of_run_for_same_due_sends_nothing(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="null_check",
                    ran_at=clock.now(),
                    detail={},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            people = await on_call.list("prj_1")
            msg = Message(title="Alert", severity="critical")
            await notifier.notify("prj_1", people[0], msg, kind="incident", incident_id=incident_id)

            clock.advance(timedelta(minutes=15))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert len(dues) == 1
            job = dues[0]

            # First run: sends escalation to Bob
            await escalation_job.run("prj_1", job)

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).all()
            assert len(rows) == 2

            # Re-run of run for the SAME due sends nothing!
            await escalation_job.run("prj_1", job)

            async with engine.connect() as conn:
                rows_after = (await conn.execute(select(notification_log))).all()
            assert len(rows_after) == 2  # No new row
    finally:
        await databases.dispose()


async def test_downtime_gap_catchup_sends_one_escalation(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="gap_check",
                    ran_at=clock.now(),
                    detail={},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            people = await on_call.list("prj_1")
            msg = Message(title="Alert", severity="critical")
            await notifier.notify("prj_1", people[0], msg, kind="incident", incident_id=incident_id)

            # Advance clock by 2 hours (120 minutes) simulating downtime
            clock.advance(timedelta(hours=2))

            # due() returns ONE escalation, not one per missed minute!
            dues = await escalation_job.due("prj_1", clock.now() - timedelta(hours=2), clock.now())
            assert len(dues) == 1
            assert dues[0].subject == incident_id

            # Run catch-up escalation
            await escalation_job.run("prj_1", dues[0])

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).all()
            assert len(rows) == 2  # Alice step 0 + Bob step 1 (only 1 escalation sent)

            # Immediately checking due returns empty (next person is not due for another 15m)
            dues_next = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=1), clock.now()
            )
            assert dues_next == []
    finally:
        await databases.dispose()


async def test_past_last_person_nothing_sent(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]
    on_call = env["on_call"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            escalation_job = EscalationJob(
                databases=databases,
                incidents=incidents,
                on_call=on_call,
                notifier=notifier,
                clock=clock,
            )

            outcome = await incidents.raise_signal(
                "prj_1",
                SignalDraft(
                    kind="fired_result",
                    category="data_quality",
                    subject="exhaust_check",
                    ran_at=clock.now(),
                    detail={},
                ),
                seen_at=clock.now(),
            )
            incident_id = outcome.incident_id

            people = await on_call.list("prj_1")
            # Step 0: Alice
            msg = Message(title="Alert", severity="critical")
            await notifier.notify("prj_1", people[0], msg, kind="incident", incident_id=incident_id)

            # Step 1: Bob (after 15m)
            clock.advance(timedelta(minutes=15))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert len(dues) == 1
            await escalation_job.run("prj_1", dues[0])

            # Step 2: Charlie (after another 15m)
            clock.advance(timedelta(minutes=15))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert len(dues) == 1
            await escalation_job.run("prj_1", dues[0])

            # Now all 3 on-call people have been sent!
            # Advance another 15m
            clock.advance(timedelta(minutes=15))
            dues = await escalation_job.due(
                "prj_1", clock.now() - timedelta(minutes=15), clock.now()
            )
            assert dues == []  # Past last person, nothing due!

            dummy = JobDue(kind=JobKind.ESCALATE, due=clock.now(), subject=incident_id)
            await escalation_job.run("prj_1", dummy)

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).all()
            assert len(rows) == 3  # Still only 3 rows
    finally:
        await databases.dispose()


async def test_channel_failure_signal_after_consecutive_failures(
    tmp_path: Path, fake_discord: RunningFakeDiscord, fake_smtp: RunningFakeSmtp
) -> None:
    env = await setup_test_env(tmp_path, fake_discord, fake_smtp)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]

    try:
        # Make Discord fail
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            notifier.bind_incidents(incidents)
            job = RetryJob(databases, notifier, clock)

            p_discord = OnCallPerson(
                id="disc_user",
                name="Disc User",
                contacts=[Contact(channel="discord", address="u_disc")],
                hours=OnCallHours(days=["mon"], start="09:00", end="17:00"),
                timezone="UTC",
                escalation_order=1,
            )
            p_email = OnCallPerson(
                id="email_user",
                name="Email User",
                contacts=[Contact(channel="email", address="user@example.com")],
                hours=OnCallHours(days=["mon"], start="09:00", end="17:00"),
                timezone="UTC",
                escalation_order=2,
            )

            msg = Message(title="Alert", severity="high")

            # Helper to run a delivery through all its backoffs until it reaches 'failed'
            async def deliver_and_exhaust_retries(person: OnCallPerson, inc_id: str) -> None:
                res = await notifier.notify(
                    "prj_1", person, msg, kind="incident", incident_id=inc_id
                )
                assert res == "retrying"
                # Retry 1 (30s)
                clock.advance(timedelta(seconds=35))
                await job.run("prj_1", None)  # type: ignore
                # Retry 2 (120s)
                clock.advance(timedelta(seconds=125))
                await job.run("prj_1", None)  # type: ignore
                # Retry 3 (600s)
                clock.advance(timedelta(seconds=605))
                await job.run("prj_1", None)  # type: ignore

            # 1st failed delivery on Discord
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            await deliver_and_exhaust_retries(p_discord, "inc_1")

            page = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page.rows) == 0  # Not yet 3 failures

            # A successful email delivery happens in between -> should not reset Discord's failure count!
            res_email = await notifier.notify(
                "prj_1", p_email, msg, kind="incident", incident_id="inc_email"
            )
            assert res_email == "sent"

            # 2nd failed delivery on Discord
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            await deliver_and_exhaust_retries(p_discord, "inc_2")

            page = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page.rows) == 0

            # 3rd failed delivery on Discord -> FAILURE_SIGNAL_AFTER reached!
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            await deliver_and_exhaust_retries(p_discord, "inc_3")

            page = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page.rows) == 1
            fail_inc = page.rows[0]
            assert fail_inc.category == "operational"
            assert fail_inc.cause_key == cause_key("channel:discord", None)

            # 4th failed delivery on Discord -> attaches to the SAME incident
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            await deliver_and_exhaust_retries(p_discord, "inc_4")

            page = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page.rows) == 1  # Still 1 incident
            sigs = await incidents.signals("prj_1", fail_inc.id)
            assert len(sigs) == 2  # 3rd and 4th failure signals attached to same incident
    finally:
        await databases.dispose()


async def test_failure_while_delivering_channel_failure_incident_raises_nothing_more(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    env = await setup_test_env(tmp_path, fake_discord)
    databases = env["databases"]
    settings = env["settings"]
    events = env["events"]
    clock = env["clock"]
    incidents = env["incidents"]

    try:
        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            notifier.bind_incidents(incidents)
            job = RetryJob(databases, notifier, clock)

            p_discord = OnCallPerson(
                id="disc_user",
                name="Disc User",
                contacts=[Contact(channel="discord", address="u_disc")],
                hours=OnCallHours(days=["mon"], start="09:00", end="17:00"),
                timezone="UTC",
                escalation_order=1,
            )
            msg = Message(title="Alert", severity="high")

            async def deliver_and_exhaust_retries(person: OnCallPerson, inc_id: str) -> None:
                res = await notifier.notify(
                    "prj_1", person, msg, kind="incident", incident_id=inc_id
                )
                assert res == "retrying"
                clock.advance(timedelta(seconds=35))
                await job.run("prj_1", None)  # type: ignore
                clock.advance(timedelta(seconds=125))
                await job.run("prj_1", None)  # type: ignore
                clock.advance(timedelta(seconds=605))
                await job.run("prj_1", None)  # type: ignore

            # Trigger channel failure incident on Discord (3 failures)
            for i in range(3):
                fake_discord.state.fail_next(500)
                fake_discord.state.fail_next(500)
                fake_discord.state.fail_next(500)
                fake_discord.state.fail_next(500)
                await deliver_and_exhaust_retries(p_discord, f"inc_setup_{i}")

            page = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page.rows) == 1
            fail_inc_id = page.rows[0].id
            signals_before = await incidents.signals("prj_1", fail_inc_id)
            assert len(signals_before) == 1

            # Now deliver for fail_inc_id itself, which ALSO fails on Discord:
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            fake_discord.state.fail_next(500)
            await deliver_and_exhaust_retries(p_discord, fail_inc_id)

            # "A failure while delivering that incident's own notifications raises nothing more."
            signals_after = await incidents.signals("prj_1", fail_inc_id)
            assert len(signals_after) == 1  # No additional signal added!
            page_after = await incidents.list("prj_1", status=["open", "reopened"])
            assert len(page_after.rows) == 1  # No new incident opened!
    finally:
        await databases.dispose()
