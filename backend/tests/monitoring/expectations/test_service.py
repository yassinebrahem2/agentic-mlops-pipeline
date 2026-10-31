"""Tests for ExpectationsService, silence evaluation and states."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.monitoring.expectations.contract import (
    SECOND_DEADLINE_KEY,
    ExpectationKey,
    ExpectationSpec,
)
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.contract import StoredResult
from mlops_agents.monitoring.settings import MonitoringSettings, set_monitoring_settings
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import Cadence, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import CiRunRow, Page, TrackingQuery
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.proposals.samples import handoff, job_feed
from tests.providers.fake_clock import FakeClock


class FakeIncidents:
    def __init__(self) -> None:
        self.signals: list[tuple[str, SignalDraft, datetime]] = []

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome:
        self.signals.append((project_id, draft, seen_at))
        return SignalOutcome(
            signal_id=f"sig_{len(self.signals)}",
            incident_id=f"inc_{len(self.signals)}",
            change="opened",
            severity="medium",
        )


class FakeMaintenance:
    def __init__(self, suspended_keys: set[str] | None = None) -> None:
        self.suspended_keys = suspended_keys or set()

    async def suspended(self, project_id: str, scope: Any, at: datetime) -> bool:
        if isinstance(scope, ExpectationKey):
            return scope.ref in self.suspended_keys or str(scope) in self.suspended_keys
        return str(scope) in self.suspended_keys


class FakeTracking:
    def __init__(self, ci_runs: list[CiRunRow] | None = None) -> None:
        self._ci_runs = ci_runs or []

    def set_runs(self, runs: list[CiRunRow]) -> None:
        self._ci_runs = runs

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "ci_runs":
            runs = sorted(self._ci_runs, key=lambda r: r.start, reverse=True)
            return Page(rows=runs, total=len(runs), truncated=False)
        return Page(rows=[], total=0, truncated=False)


@pytest.fixture
def project_id() -> str:
    return "prj_testexpectations"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


def _make_spec(
    ref: str,
    kind: str = "feed",
    cadence: Cadence | None = None,
    due_at: datetime | None = None,
    grace_minutes: int | None = 30,
    second_deadline: bool = True,
    model: str | None = None,
    source: str = "handoff",
) -> ExpectationSpec:
    return ExpectationSpec(
        key=ExpectationKey(kind=kind, ref=ref, model=model),  # type: ignore
        category="data_quality" if kind == "feed" else "operational",
        cadence=cadence,
        due_at=due_at,
        timezone="UTC",
        grace_minutes=grace_minutes,
        second_deadline=second_deadline,
        source=source,  # type: ignore
    )


async def test_job_result_inside_grace_satisfies_occurrence(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )

    # Hourly job at :00, grace 30 min.
    spec = _make_spec(
        "job_batch",
        kind="job",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
        second_deadline=True,
    )
    await service.register(project_id, spec)

    # Occurrence is 12:00. Result arrives at 12:15.
    ran_at = datetime(2026, 10, 31, 12, 15, tzinfo=UTC)
    result = StoredResult(
        id="res_1",
        check_id="job_batch",
        origin="agent",
        ran_at=ran_at,
        fired=False,
        category="operational",
        subject="job_batch",
        model=None,
        detail={},
        source_ref="jobs/batch.py",
        source_id="ci:batch.yml",
        source_key="k1",
        seen_at=ran_at,
    )
    await service.results_observer.stored(project_id, [result])

    # Advance clock to 12:45 (past grace).
    clock.current = datetime(2026, 10, 31, 12, 45, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())
    assert len(signals) == 0
    assert len(incidents.signals) == 0


async def test_missing_result_past_deadline_and_grace_raises_signal(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )

    # Check expectation: daily at 12:00 UTC, grace None (uses default check_grace_minutes=30).
    spec = _make_spec(
        "chk_daily",
        kind="check",
        cadence=Cadence(cron="0 12 * * *", every_minutes=None),
        grace_minutes=None,
        second_deadline=False,
    )
    # Check seen yesterday at 12:00
    yesterday = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    await service.register(project_id, spec)
    await service.note_seen(project_id, spec.key, yesterday)

    # Advance to 12:35 UTC (past 12:00 + 30m grace)
    clock.current = datetime(2026, 10, 31, 12, 35, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())
    assert len(signals) == 1
    assert len(incidents.signals) == 1

    _, draft, _seen_at = incidents.signals[0]
    assert draft.kind == "silence"
    assert draft.category == "operational"
    assert draft.subject == "chk_daily"
    assert draft.detail["occurrence"] == "2026-10-31T12:00:00+00:00"
    assert draft.expectation == spec.key

    # A second run raises none!
    signals_second = await service.evaluate(project_id, clock.now())
    assert len(signals_second) == 0
    assert len(incidents.signals) == 1


async def test_second_deadline_raises_more_for_feed_but_check_raises_no_second(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )
    await set_monitoring_settings(
        databases,
        project_id,
        MonitoringSettings(check_grace_minutes=30, second_deadline_minutes=60),
    )

    # Job feed (second_deadline=True) and Check (second_deadline=False)
    feed_spec = _make_spec(
        "feed_hourly",
        kind="feed",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
        second_deadline=True,
    )
    chk_spec = _make_spec(
        "chk_hourly",
        kind="check",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
        second_deadline=False,
    )
    t_prev = datetime(2026, 10, 31, 11, 0, tzinfo=UTC)
    await service.register(project_id, feed_spec)
    await service.register(project_id, chk_spec)
    await service.note_seen(project_id, feed_spec.key, t_prev)
    await service.note_seen(project_id, chk_spec.key, t_prev)

    # Occurrence is 12:00. At 12:35 (first deadline: 12:00 + 30m): both raise one signal.
    clock.current = datetime(2026, 10, 31, 12, 35, tzinfo=UTC)
    await service.evaluate(project_id, clock.now())
    assert len(incidents.signals) == 2

    # Advance past second deadline (12:00 + 30m + 60m = 13:30) to 13:35.
    clock.current = datetime(2026, 10, 31, 13, 35, tzinfo=UTC)
    await service.evaluate(project_id, clock.now())

    # Only feed_spec raises a second signal for the 12:00 occurrence!
    # (plus first signals for 13:00 occurrence)
    second_deadline_signals = [
        s for _, s, _ in incidents.signals if s.detail.get(SECOND_DEADLINE_KEY) is True
    ]
    assert len(second_deadline_signals) == 1
    assert second_deadline_signals[0].subject == "feed_hourly"


async def test_ten_hour_gap_raises_each_miss_oldest_first_and_arrived_feed_raises_nothing(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )
    # Check with hourly cadence
    chk_spec = _make_spec(
        "chk_hourly_drift",
        kind="check",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=15,
        second_deadline=False,
    )
    # Feed with daily cadence whose data arrived during gap
    feed_spec = _make_spec(
        "feed_data_fresh",
        kind="feed",
        cadence=Cadence(cron=None, every_minutes=360),  # every 6 hours
        grace_minutes=30,
        second_deadline=True,
    )
    # Registered and seen at 00:00
    t0 = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    clock.current = t0
    await service.register(project_id, chk_spec)
    await service.register(project_id, feed_spec)
    await service.note_seen(project_id, chk_spec.key, t0)
    await service.note_seen(project_id, feed_spec.key, t0)

    # During the 10-hour gap, data arrived for feed_data_fresh at 06:10 (within 06:00 occurrence window)
    t_feed_arrived = datetime(2026, 10, 31, 6, 10, tzinfo=UTC)
    await service.note_seen(project_id, feed_spec.key, t_feed_arrived)

    # App evaluates at 10:00 (10 hour gap)
    clock.current = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    await service.evaluate(project_id, clock.now())

    # Feed raised nothing!
    feed_signals = [s for _, s, _ in incidents.signals if s.expectation == feed_spec.key]
    assert len(feed_signals) == 0

    # Check raised missed occurrences in chronological order (oldest first)
    chk_signals = [s for _, s, _ in incidents.signals if s.expectation == chk_spec.key]
    assert len(chk_signals) >= 8  # 01:00, 02:00, ..., 09:00
    occurrences = [s.detail["occurrence"] for s in chk_signals]
    assert occurrences == sorted(occurrences)


async def test_job_running_past_grace_raises_nothing(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    tracking = FakeTracking()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        tracking=tracking,
        clock=clock,
    )

    # Job feed configured with ci_workflow="nightly.yml"
    feed_spec = _make_spec(
        "nightly_job",
        kind="job",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
        second_deadline=True,
    )
    raw = handoff()
    raw["operations"]["feeds"] = [
        job_feed(name="nightly_job", ref={"ci_workflow": "nightly.yml", "file": None})
    ]
    raw["operations"]["checks"] = []
    handoffs.set_handoff(
        HandoffView(
            proposal_id="p1",
            version=1,
            approved=datetime(2026, 10, 30, 0, 0, tzinfo=UTC),
            payload=HandoffPayload.model_validate(raw),
        )
    )
    await service.register(project_id, feed_spec)

    # Job started at 12:15, currently running past grace at 12:35
    running_run = CiRunRow(
        source_id="ci:nightly.yml",
        key="run_999",
        workflow="nightly.yml",
        status="running",
        start=datetime(2026, 10, 31, 12, 15, tzinfo=UTC),
        duration_s=None,
        commit="abc1234",
        url="https://ci/999",
    )
    tracking.set_runs([running_run])

    clock.current = datetime(2026, 10, 31, 12, 35, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())
    assert len(signals) == 0
    assert len(incidents.signals) == 0


async def test_suspended_key_raises_nothing(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    maintenance = FakeMaintenance(suspended_keys={"job_maintenance"})
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        maintenance=maintenance,
        clock=clock,
    )

    spec = _make_spec(
        "job_maintenance",
        kind="job",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
    )
    await service.register(project_id, spec)

    # 12:45 is past deadline and grace, but key is suspended
    clock.current = datetime(2026, 10, 31, 12, 45, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())
    assert len(signals) == 0
    assert len(incidents.signals) == 0

    # States shows suspended
    states = await service.states(project_id)
    assert len(states) == 1
    assert states[0].status == "suspended"


async def test_due_at_expectation_raises_once(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )

    due_time = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    spec = _make_spec(
        "tr_due",
        kind="training_request",
        due_at=due_time,
        grace_minutes=30,
        second_deadline=False,
    )
    await service.register(project_id, spec)

    # Advance past due_at + grace (12:30) to 12:45
    clock.current = datetime(2026, 10, 31, 12, 45, tzinfo=UTC)
    signals1 = await service.evaluate(project_id, clock.now())
    assert len(signals1) == 1
    assert len(incidents.signals) == 1

    # Second evaluation raises nothing
    signals2 = await service.evaluate(project_id, clock.now())
    assert len(signals2) == 0
    assert len(incidents.signals) == 1


async def test_cadence_across_dst_change_misses_only_real_misses(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )

    # Daily at 02:30 in Europe/Paris across spring forward (2026-03-29).
    # 02:30 does not exist on that day.
    spec = ExpectationSpec(
        key=ExpectationKey(kind="check", ref="chk_paris_dst"),
        category="operational",
        cadence=Cadence(cron="30 2 * * *", every_minutes=None),
        timezone="Europe/Paris",
        grace_minutes=30,
        source="check",
    )
    # Registered and seen on 2026-03-28 at 02:30 Paris (01:30 UTC)
    clock.current = datetime(2026, 3, 28, 1, 30, tzinfo=UTC)
    await service.register(project_id, spec, active=True)
    await service.note_seen(project_id, spec.key, datetime(2026, 3, 28, 1, 30, tzinfo=UTC))

    # Advance to 2026-03-29 05:00 UTC (after non-existent 02:30)
    clock.current = datetime(2026, 3, 29, 5, 0, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())
    # The non-existent 02:30 Paris was skipped by next_occurrences, so nothing missed on the 29th
    assert len(signals) == 0


async def test_feed_miss_followed_by_multiple_successful_arrivals_emits_no_spurious_signals(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    connections = FakeConnections()
    handoffs = FakeHandoffs()
    service = ExpectationsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        incidents=incidents,
        clock=clock,
    )

    # Hourly feed at :00, grace 30m, second deadline enabled
    spec = _make_spec(
        "feed_pipeline",
        kind="feed",
        cadence=Cadence(cron=None, every_minutes=60),
        grace_minutes=30,
        second_deadline=True,
    )
    await service.register(project_id, spec, active=True)

    # Initial seen at 11:00 (satisfies 11:00 occurrence, but not 12:00)
    t0 = datetime(2026, 10, 31, 11, 0, tzinfo=UTC)
    await service.note_seen(project_id, spec.key, t0)

    # 1. Occurrence at 12:00 misses. At 12:45 evaluate runs -> 1 signal raised.
    clock.current = datetime(2026, 10, 31, 12, 45, tzinfo=UTC)
    signals1 = await service.evaluate(project_id, clock.now())
    assert len(signals1) == 1
    assert len(incidents.signals) == 1
    assert incidents.signals[0][1].detail["occurrence"] == "2026-10-31T12:00:00+00:00"

    # 2. Occurrence at 13:00 arrives on time at 13:05.
    t_1300_seen = datetime(2026, 10, 31, 13, 5, tzinfo=UTC)
    await service.note_seen(project_id, spec.key, t_1300_seen)

    clock.current = datetime(2026, 10, 31, 13, 45, tzinfo=UTC)
    signals2 = await service.evaluate(project_id, clock.now())
    # No new signals raised
    assert len(signals2) == 0
    assert len(incidents.signals) == 1

    # 3. Occurrence at 14:00 arrives on time at 14:05.
    t_1400_seen = datetime(2026, 10, 31, 14, 5, tzinfo=UTC)
    await service.note_seen(project_id, spec.key, t_1400_seen)

    clock.current = datetime(2026, 10, 31, 14, 45, tzinfo=UTC)
    signals3 = await service.evaluate(project_id, clock.now())
    # Crucial test: older 13:00 occurrence must not be re-evaluated as a miss now that
    # last_seen has advanced to 14:05.
    assert len(signals3) == 0
    assert len(incidents.signals) == 1

    # 4. Occurrence at 15:00 arrives on time at 15:05.
    t_1500_seen = datetime(2026, 10, 31, 15, 5, tzinfo=UTC)
    await service.note_seen(project_id, spec.key, t_1500_seen)

    clock.current = datetime(2026, 10, 31, 15, 45, tzinfo=UTC)
    signals4 = await service.evaluate(project_id, clock.now())
    assert len(signals4) == 0
    assert len(incidents.signals) == 1
