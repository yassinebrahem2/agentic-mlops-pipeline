"""Tests for signals, incidents opening and attaching, and SignalsFromResults."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import cause_key
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.storage.databases import Databases
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
    make_stored_result,
)


@pytest.fixture
def service(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_results: FakeResults,
    fake_severity: FakeSeverityPolicy,
    fake_maintenance: FakeMaintenance,
    fake_clock: FakeClock,
) -> IncidentsService:
    return IncidentsService(
        databases=databases,
        events=event_log,
        results=fake_results,
        severity=fake_severity,
        maintenance=fake_maintenance,
        clock=fake_clock,
    )


@pytest.fixture
def observer(service: IncidentsService, fake_maintenance: FakeMaintenance) -> SignalsFromResults:
    return SignalsFromResults(incidents=service, maintenance=fake_maintenance)


@pytest.mark.asyncio
async def test_fired_agent_record_opens_incident_and_second_attaches(
    service: IncidentsService,
    observer: SignalsFromResults,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    t1_ran = datetime(2026, 10, 30, 9, 30, 0, tzinfo=UTC)
    t1_seen = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    res1 = make_stored_result(
        "res_001",
        ran_at=t1_ran,
        seen_at=t1_seen,
        category="drift",
        subject="predictions_drift",
        model="churn_model",
    )

    await observer.stored(project_id, [res1])

    incidents_page = await service.list(project_id)
    assert incidents_page.total == 1
    inc = incidents_page.rows[0]

    expected_key = cause_key("predictions_drift", "churn_model")
    assert inc.cause_key == expected_key
    assert inc.category == "drift"
    assert inc.model == "churn_model"
    assert inc.status == "open"
    assert inc.started_at == t1_ran
    assert inc.detected_at == t1_seen
    assert len(inc.signal_ids) == 1

    events = await event_log.page(project_id, subject_id=inc.id)
    opened_events = [e for e in events if e.type == "incident.opened"]
    assert len(opened_events) == 1
    assert opened_events[0].subject_id == inc.id

    t2_ran = datetime(2026, 10, 30, 9, 15, 0, tzinfo=UTC)
    t2_seen = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
    res2 = make_stored_result(
        "res_002",
        ran_at=t2_ran,
        seen_at=t2_seen,
        category="drift",
        subject="predictions_drift",
        model="churn_model",
    )

    await observer.stored(project_id, [res2])

    incidents_page2 = await service.list(project_id)
    assert incidents_page2.total == 1
    inc2 = incidents_page2.rows[0]
    assert inc2.id == inc.id
    assert inc2.started_at == t2_ran
    assert inc2.detected_at == t1_seen
    assert len(inc2.signal_ids) == 2


@pytest.mark.asyncio
async def test_team_non_fired_and_not_enough_data_raise_nothing(
    service: IncidentsService,
    observer: SignalsFromResults,
    project_id: str,
) -> None:
    res_team = make_stored_result("res_team", origin="team", fired=True)
    res_not_fired = make_stored_result("res_unfired", origin="agent", fired=False)
    res_ned = make_stored_result("res_ned", origin="agent", fired=True, not_enough_data=True)

    await observer.stored(project_id, [res_team, res_not_fired, res_ned])

    page = await service.list(project_id)
    assert page.total == 0


@pytest.mark.asyncio
async def test_batch_raised_oldest_first(
    service: IncidentsService,
    observer: SignalsFromResults,
    project_id: str,
) -> None:
    t_early = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)
    t_late = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)

    res_late = make_stored_result("res_late", ran_at=t_late, subject="subj_order")
    res_early = make_stored_result("res_early", ran_at=t_early, subject="subj_order")

    await observer.stored(project_id, [res_late, res_early])

    page = await service.list(project_id)
    assert page.total == 1
    inc = page.rows[0]
    signals = await service.signals(project_id, inc.id)
    assert len(signals) == 2
    assert signals[0].ran_at == t_early
    assert signals[0].result_id == "res_early"
    assert signals[1].ran_at == t_late
    assert signals[1].result_id == "res_late"


@pytest.mark.asyncio
async def test_same_result_delivered_twice_raises_one_signal(
    service: IncidentsService,
    observer: SignalsFromResults,
    project_id: str,
) -> None:
    res = make_stored_result("res_dup")

    await observer.stored(project_id, [res])
    await observer.stored(project_id, [res])

    page = await service.list(project_id)
    assert page.total == 1
    inc = page.rows[0]
    signals = await service.signals(project_id, inc.id)
    assert len(signals) == 1
    assert signals[0].result_id == "res_dup"


@pytest.mark.asyncio
async def test_record_in_suspended_scope_raises_nothing(
    service: IncidentsService,
    observer: SignalsFromResults,
    fake_maintenance: FakeMaintenance,
    project_id: str,
) -> None:
    fake_maintenance.suspended_scopes.add("conn_maint")

    res = make_stored_result(
        "res_suspended",
        source_id="file:conn_maint:data/*.parquet",
    )

    await observer.stored(project_id, [res])

    page = await service.list(project_id)
    assert page.total == 0
