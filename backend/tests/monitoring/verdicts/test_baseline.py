"""SqliteBaseline: team fires linked to incidents both ways, silent alerts and their verdicts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import update

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    IncidentStatus,
    IncidentView,
    MediumSeverity,
)
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.contract import (
    ResultOrigin,
    ResultQuery,
    ResultRecord,
    StoredResult,
)
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.baseline import (
    BaselineIncidentObserver,
    BaselineResultsObserver,
    SqliteBaseline,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import incidents as incidents_table
from mlops_agents.tracking.contract import Page
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.monitoring.verdicts.conftest import SOURCE, T0, open_incident, verdict_events
from tests.providers.fake_clock import FakeClock

SUBJECT = "churn_scores"
_keys = count()


@pytest.fixture
def baseline(
    databases: Databases,
    results: ResultsService,
    incidents: IncidentsService,
    events: SqliteEventLog,
    clock: FakeClock,
) -> SqliteBaseline:
    """The baseline wired as build_services wires it: beside SignalsFromResults."""
    store = SqliteBaseline(databases, results, incidents, events, clock)
    results.add_observer(SignalsFromResults(incidents, NeverSuspended()))
    results.add_observer(BaselineResultsObserver(store))
    incidents.add_observer(BaselineIncidentObserver(store))
    return store


def _record(
    ran_at: datetime,
    *,
    subject: str,
    check_id: str,
    origin: ResultOrigin,
    fired: bool,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin=origin,
        ran_at=ran_at,
        fired=fired,
        category="model_performance",
        subject=subject,
        model="churn",
        detail={},
        source_ref=f"results.jsonl:{check_id}",
    )


async def _store(
    results: ResultsService,
    project_id: str,
    ran_at: datetime,
    *,
    subject: str = SUBJECT,
    check_id: str = "team_recall",
    origin: ResultOrigin = "team",
    fired: bool = True,
    seen_at: datetime | None = None,
) -> StoredResult:
    record = _record(ran_at, subject=subject, check_id=check_id, origin=origin, fired=fired)
    stored = await results.ingest(
        project_id, SOURCE, [(f"b{next(_keys)}", record)], seen_at or ran_at
    )
    return stored[0]


async def _deliver(results: ResultsService, project_id: str, clock: FakeClock) -> None:
    await results.deliver_undelivered(project_id, clock.now())


async def _team_fire(
    results: ResultsService,
    project_id: str,
    clock: FakeClock,
    ran_at: datetime,
    *,
    subject: str = SUBJECT,
    check_id: str = "team_recall",
) -> StoredResult:
    stored = await _store(results, project_id, ran_at, subject=subject, check_id=check_id)
    await _deliver(results, project_id, clock)
    return stored


async def test_a_team_fire_before_an_agent_incident_is_linked_with_a_negative_lead(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    fire = await _team_fire(results, project_id, clock, T0 - timedelta(minutes=10))
    assert [a.result_id for a in await baseline.silent_alerts(project_id)] == [fire.id]

    incident_id = await open_incident(incidents, project_id, started_at=T0)

    [link] = await baseline.links(project_id, incident_id)
    assert link.incident_id == incident_id
    assert link.team_check_id == "team_recall"
    assert link.first_fired_at == fire.ran_at
    assert link.seen_at == fire.seen_at
    assert link.lead_seconds == -600
    # Once an incident matches it, the fire is no longer silent.
    assert await baseline.silent_alerts(project_id) == []


async def test_a_team_fire_after_an_agent_incident_is_linked_with_a_positive_lead(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0)
    clock.advance(1200)

    fire = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=10))

    [link] = await baseline.links(project_id, incident_id)
    assert link.first_fired_at == fire.ran_at
    assert link.lead_seconds == 600
    assert await baseline.silent_alerts(project_id) == []


async def test_a_team_fire_on_another_key_or_outside_the_window_links_nothing(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0)
    clock.advance(3600)

    early = await _team_fire(results, project_id, clock, T0 - timedelta(minutes=61))
    other = await _team_fire(
        results, project_id, clock, T0 + timedelta(minutes=5), subject="other_table"
    )

    assert await baseline.links(project_id, incident_id) == []
    silent = await baseline.silent_alerts(project_id)
    assert [a.result_id for a in silent] == [other.id, early.id]


async def test_a_repeated_delivery_or_a_reopen_adds_no_second_link(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0)
    clock.advance(3600)
    fire = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=20))
    [first] = await baseline.links(project_id, incident_id)

    await BaselineResultsObserver(baseline).stored(project_id, [fire])
    reopen = IncidentChange(incident_id=incident_id, kind="reopened")
    await BaselineIncidentObserver(baseline).changed(project_id, reopen)
    await BaselineIncidentObserver(baseline).changed(project_id, reopen)

    assert await baseline.links(project_id, incident_id) == [first]


async def test_a_reopen_looks_back_for_a_fire_after_the_old_resolved_at(
    baseline: SqliteBaseline,
    databases: Databases,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0, resolved=True)
    clock.advance(3600)
    fire = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=30))
    assert await baseline.links(project_id, incident_id) == []

    # Nothing reopens an incident through a service yet: the row is set as a reopen leaves it,
    # with its old resolved_at kept.
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(incidents_table)
            .where(incidents_table.c.id == incident_id)
            .values(status="reopened")
        )
    await BaselineIncidentObserver(baseline).changed(
        project_id, IncidentChange(incident_id=incident_id, kind="reopened")
    )

    [link] = await baseline.links(project_id, incident_id)
    assert link.first_fired_at == fire.ran_at
    assert link.lead_seconds == 1800


async def test_an_earlier_fire_arriving_later_replaces_the_first_fire_and_its_lead(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0)
    clock.advance(3600)
    await _team_fire(results, project_id, clock, T0 + timedelta(minutes=20))

    caught_up = await _store(
        results, project_id, T0 + timedelta(minutes=5), seen_at=T0 + timedelta(minutes=50)
    )
    await _deliver(results, project_id, clock)
    later = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=30))

    [link] = await baseline.links(project_id, incident_id)
    assert later.ran_at > link.first_fired_at
    assert link.first_fired_at == caught_up.ran_at
    assert link.seen_at == caught_up.seen_at
    assert link.lead_seconds == 300


async def test_a_fire_on_a_merged_incidents_key_links_to_its_survivor(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    survivor_id = await open_incident(incidents, project_id, subject="features", started_at=T0)
    merged_id = await open_incident(
        incidents, project_id, subject=SUBJECT, started_at=T0 + timedelta(minutes=30)
    )
    merged = await incidents.merge(project_id, [survivor_id, merged_id], agent="orchestrator")
    assert merged.id == survivor_id
    clock.advance(7200)

    fire = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=40))

    [link] = await baseline.links(project_id, survivor_id)
    assert link.first_fired_at == fire.ran_at
    assert link.lead_seconds == 2400  # from the survivor's detected_at
    assert await baseline.links(project_id, merged_id) == []
    assert await baseline.silent_alerts(project_id) == []


async def test_an_incident_a_person_closed_while_open_stops_matching_after_closed_at(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id, started_at=T0)
    clock.advance(3600)
    await incidents.close(project_id, incident_id, by="user")
    clock.advance(3600)

    after = await _team_fire(results, project_id, clock, T0 + timedelta(minutes=90))
    before = await _team_fire(
        results, project_id, clock, T0 + timedelta(minutes=30), check_id="team_precision"
    )

    [link] = await baseline.links(project_id, incident_id)
    assert link.team_check_id == "team_precision"
    assert link.first_fired_at == before.ran_at
    assert [a.result_id for a in await baseline.silent_alerts(project_id)] == [after.id]


async def test_silent_alerts_are_newest_first_and_capped_at_500_silent_ones(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    await open_incident(incidents, project_id, subject="matched", started_at=T0)
    clock.advance(3600)
    for minutes in (1, 2, 3):
        await _store(results, project_id, T0 + timedelta(minutes=minutes), subject="matched")
    silent = [
        (
            f"s{n}",
            _record(
                T0 - timedelta(hours=2, seconds=n),
                subject=SUBJECT,
                check_id="team_recall",
                origin="team",
                fired=True,
            ),
        )
        for n in range(501)
    ]
    await results.ingest(project_id, SOURCE, silent, T0)

    listed = await baseline.silent_alerts(project_id)

    assert len(listed) == 500
    assert {a.subject for a in listed} == {SUBJECT}
    assert [a.ran_at for a in listed] == sorted((a.ran_at for a in listed), reverse=True)
    assert listed[0].ran_at == T0 - timedelta(hours=2)


async def test_a_silent_alert_verdict_is_stored_listed_and_replaced(
    baseline: SqliteBaseline,
    results: ResultsService,
    events: SqliteEventLog,
    project_id: str,
    clock: FakeClock,
) -> None:
    fire = await _team_fire(results, project_id, clock, T0 - timedelta(minutes=5))

    judged = await baseline.judge_silent(project_id, fire.id, "real_issue", by="user")
    assert judged.result_id == fire.id
    assert judged.check_id == "team_recall"
    assert judged.subject == SUBJECT
    assert judged.ran_at == fire.ran_at
    assert judged.verdict == "real_issue"
    assert await baseline.silent_alerts(project_id) == [judged]

    replaced = await baseline.judge_silent(project_id, fire.id, "not_an_issue", by="user")
    assert replaced.verdict == "not_an_issue"
    assert await baseline.silent_alerts(project_id) == [replaced]

    recorded = await verdict_events(events, project_id)
    assert [(e.source, e.subject_id) for e in recorded] == [("user", fire.id)] * 2
    assert [e.payload["value"] for e in recorded] == ["not_an_issue", "real_issue"]
    assert recorded[0].payload == {
        "value": "not_an_issue",
        "source": "user",
        "kind": "silent_alert",
    }


async def test_a_verdict_on_anything_but_a_silent_team_alert_is_404(
    baseline: SqliteBaseline,
    results: ResultsService,
    incidents: IncidentsService,
    project_id: str,
    clock: FakeClock,
) -> None:
    await open_incident(incidents, project_id, subject="matched", started_at=T0)
    matched = await _team_fire(results, project_id, clock, T0, subject="matched")
    agent = await _store(results, project_id, T0, subject="agent_only", origin="agent")
    quiet = await _store(results, project_id, T0, subject="quiet", fired=False)

    for result_id in ("res_unknown", matched.id, agent.id, quiet.id):
        with pytest.raises(DomainError) as raised:
            await baseline.judge_silent(project_id, result_id, "real_issue", by="user")
        assert raised.value.code == "silent_alert_not_found"
        assert raised.value.status == 404


async def test_available_flips_with_the_first_team_record(
    baseline: SqliteBaseline, results: ResultsService, project_id: str
) -> None:
    assert not await baseline.available(project_id)
    await _store(results, project_id, T0, origin="agent")
    assert not await baseline.available(project_id)

    await _store(results, project_id, T0, fired=False)

    assert await baseline.available(project_id)


class _IncidentsDown(IncidentsService):
    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        raise RuntimeError("incidents down")

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        inbox: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        raise RuntimeError("incidents down")


class _ResultsDown(ResultsService):
    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        raise RuntimeError("results down")


@pytest.fixture
def incidents_down(
    databases: Databases, events: SqliteEventLog, results: ResultsService, clock: FakeClock
) -> _IncidentsDown:
    return _IncidentsDown(
        databases=databases,
        events=events,
        results=results,
        severity=MediumSeverity(),
        maintenance=NeverSuspended(),
        clock=clock,
    )


def _warnings_naming(caplog: pytest.LogCaptureFixture, *ids: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and all(i in r.getMessage() for i in ids)
    ]


async def test_the_results_observer_logs_its_own_failure_and_returns(
    databases: Databases,
    results: ResultsService,
    incidents_down: _IncidentsDown,
    events: SqliteEventLog,
    clock: FakeClock,
    project_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = SqliteBaseline(databases, results, incidents_down, events, clock)
    fire = await _store(results, project_id, T0)
    caplog.set_level(logging.WARNING)

    await BaselineResultsObserver(broken).stored(project_id, [fire])

    assert _warnings_naming(caplog, project_id, fire.id)


async def test_the_incident_observer_logs_its_own_failure_and_returns(
    databases: Databases,
    results: ResultsService,
    incidents_down: _IncidentsDown,
    events: SqliteEventLog,
    clock: FakeClock,
    project_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = SqliteBaseline(databases, results, incidents_down, events, clock)
    caplog.set_level(logging.WARNING)

    await BaselineIncidentObserver(broken).changed(
        project_id, IncidentChange(incident_id="inc_paged", kind="opened")
    )

    assert _warnings_naming(caplog, project_id, "inc_paged")


async def test_a_failing_baseline_fails_neither_raise_signal_nor_the_delivery(
    databases: Databases,
    events: SqliteEventLog,
    incidents: IncidentsService,
    incidents_down: _IncidentsDown,
    clock: FakeClock,
    project_id: str,
) -> None:
    results = ResultsService(databases, FakeHandoffs(), FakeConnections(), events)
    results_down = _ResultsDown(databases, FakeHandoffs(), FakeConnections(), events)
    paging_side = SqliteBaseline(databases, results_down, incidents, events, clock)
    delivery_side = SqliteBaseline(databases, results, incidents_down, events, clock)
    incidents.add_observer(BaselineIncidentObserver(paging_side))
    results.add_observer(BaselineResultsObserver(delivery_side))
    results.add_observer(SignalsFromResults(incidents, NeverSuspended()))

    paged = await open_incident(incidents, project_id, subject="paged", started_at=T0)
    assert (await incidents.get(project_id, paged)) is not None

    await _store(results, project_id, T0)
    agent = await _store(results, project_id, T0, subject="agent_fire", origin="agent")
    await _deliver(results, project_id, clock)

    assert await results.store.fetch_undelivered(project_id) == []
    [signal_incident] = [i for i in (await incidents.list(project_id)).rows if i.id != paged]
    assert [s.result_id for s in await incidents.signals(project_id, signal_incident.id)] == [
        agent.id
    ]
