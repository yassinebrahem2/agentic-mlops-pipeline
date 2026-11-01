"""Tests for results delivery retry on observer failure and giving up after max attempts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.incidents.contract import MediumSeverity
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.constants import RESULT_DELIVERY_MAX_ATTEMPTS
from mlops_agents.monitoring.results.contract import (
    ResultRecord,
    ResultsObserver,
    StoredResult,
    source_id,
)
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.monitoring.verdicts.baseline import (
    BaselineIncidentObserver,
    BaselineResultsObserver,
    SqliteBaseline,
)
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs, FakeObserver
from tests.providers.fake_clock import FakeClock

T0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
SOURCE = source_id("file", "*.jsonl", "results_conn")


def _record(
    check_id: str,
    origin: str = "agent",
    fired: bool = True,
    subject: str = "churn_scores",
    model: str | None = None,
    ran_at: datetime | None = None,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin=origin,  # type: ignore[arg-type]
        ran_at=ran_at or T0,
        fired=fired,
        value=0.85,
        threshold=0.80,
        category="model_performance",
        subject=subject,
        model=model,
        detail={"metric": 0.85},
        source_ref="checks/perf.py",
        version="1.0",
        not_enough_data=False,
    )


class RaisingObserver(ResultsObserver):
    """Observer that raises an error until its failure count is exhausted."""

    def __init__(self, fail_count: int = 1) -> None:
        self.remaining_failures = fail_count
        self.calls = 0
        self.batches: list[list[StoredResult]] = []

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError(f"Observer failed (remaining: {self.remaining_failures})")
        self.batches.append(list(results))


@pytest.mark.asyncio
async def test_observer_raises_once_incident_still_opens_and_baseline_links_once(
    databases: Databases,
    project_id: str,
    clock: FakeClock,
) -> None:
    events = SqliteEventLog(databases)
    results = ResultsService(databases, FakeHandoffs(), FakeConnections(), events)
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=MediumSeverity(),
        maintenance=NeverSuspended(),
        clock=clock,
    )
    baseline = SqliteBaseline(databases, results, incidents, events, clock)

    # Wire observers: signals_from_results opens incidents, BaselineResultsObserver links team fires
    results.add_observer(SignalsFromResults(incidents, NeverSuspended()))
    results.add_observer(BaselineResultsObserver(baseline))
    incidents.add_observer(BaselineIncidentObserver(baseline))

    # Flaky observer that raises once on first delivery
    flaky = RaisingObserver(fail_count=1)
    results.add_observer(flaky)

    # Insert two results: an agent fired result (which causes an incident) and a team fired result
    # on the same subject (which links to the incident).
    store = ResultsStore(databases)
    t_agent = T0
    t_team = T0 + timedelta(minutes=5)
    r_agent = _record("chk_agent", origin="agent", fired=True, ran_at=t_agent)
    r_team = _record("chk_team", origin="team", fired=True, ran_at=t_team)

    await store.insert_items(project_id, SOURCE, [("k1", r_agent), ("k2", r_team)], seen_at=T0)

    # First delivery: flaky observer raises!
    await results.deliver_undelivered(project_id, clock.now())

    # Because flaky observer raised, rows must NOT be marked delivered yet!
    undelivered_after_first = await store.fetch_undelivered(project_id)
    assert len(undelivered_after_first) == 2

    # Second delivery: flaky observer succeeds this time
    clock.advance(60)
    await results.deliver_undelivered(project_id, clock.now())

    # Now marked delivered
    undelivered_after_second = await store.fetch_undelivered(project_id)
    assert len(undelivered_after_second) == 0

    # The incident still opened!
    open_incidents = await incidents.list(project_id)
    assert len(open_incidents.rows) == 1
    incident_id = open_incidents.rows[0].id

    # And baseline link was created exactly once!
    links = await baseline.links(project_id, incident_id)
    assert len(links) == 1
    assert links[0].team_check_id == "chk_team"


@pytest.mark.asyncio
async def test_permanently_failing_observer_given_up_after_max_attempts_newer_results_flow(
    databases: Databases,
    project_id: str,
    clock: FakeClock,
) -> None:
    events = SqliteEventLog(databases)
    results = ResultsService(databases, FakeHandoffs(), FakeConnections(), events)
    store = ResultsStore(databases)

    # Attach permanently failing observer
    permanent_fail = FakeObserver(should_raise=RuntimeError("permanent failure"))
    results.add_observer(permanent_fail)

    # Healthy observer to see which results reach it
    healthy = FakeObserver()
    results.add_observer(healthy)

    # Ingest initial result r1
    r1 = _record("chk_old", ran_at=T0)
    await store.insert_items(project_id, SOURCE, [("r1", r1)], seen_at=T0)

    # Deliver 4 times: under max attempts (5)
    for attempt in range(1, RESULT_DELIVERY_MAX_ATTEMPTS):
        await results.deliver_undelivered(project_id, clock.now())
        undelivered = await store.fetch_undelivered(project_id)
        assert len(undelivered) == 1
        assert undelivered[0].check_id == "chk_old"
        attempts = await store.get_delivery_attempts(project_id, [undelivered[0].id])
        assert attempts[undelivered[0].id] == attempt

    # 5th attempt: reaches RESULT_DELIVERY_MAX_ATTEMPTS (5)
    await results.deliver_undelivered(project_id, clock.now())

    # r1 should now be marked delivered (abandoned so it doesn't wedge delivery)
    undelivered_after_max = await store.fetch_undelivered(project_id)
    assert len(undelivered_after_max) == 0

    # Ingest a newer result r2
    r2 = _record("chk_new", ran_at=T0 + timedelta(minutes=10))
    await store.insert_items(project_id, SOURCE, [("r2", r2)], seen_at=T0 + timedelta(minutes=10))

    # Newer results keep flowing: now unblock observer
    permanent_fail.should_raise = None
    await results.deliver_undelivered(project_id, clock.now())

    # r2 was delivered to healthy observer
    delivered_checks = [r.check_id for batch in healthy.batches for r in batch]
    assert "chk_new" in delivered_checks

    # Store is clean
    assert len(await store.fetch_undelivered(project_id)) == 0
