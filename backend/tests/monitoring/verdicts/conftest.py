"""Real stores on a temp data folder for the verdicts tests, and helpers that build their data."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest

from mlops_agents.events.contract import EventRecord
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.incidents.contract import MediumSeverity, SignalDraft
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.contract import (
    ResultCategory,
    ResultOrigin,
    ResultRecord,
    StoredResult,
    source_id,
)
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.providers.fake_clock import FakeClock

T0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
SOURCE = source_id("file", "*.jsonl", "results_conn")
_keys = count()


@pytest.fixture
def project_id() -> str:
    return "prj_testverdicts"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def results(databases: Databases, events: SqliteEventLog) -> ResultsService:
    return ResultsService(databases, FakeHandoffs(), FakeConnections(), events)


@pytest.fixture
def incidents(
    databases: Databases, events: SqliteEventLog, results: ResultsService, clock: FakeClock
) -> IncidentsService:
    return IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=MediumSeverity(),
        maintenance=NeverSuspended(),
        clock=clock,
    )


@pytest.fixture
def verdicts(
    databases: Databases,
    incidents: IncidentsService,
    results: ResultsService,
    events: SqliteEventLog,
    clock: FakeClock,
) -> SqliteVerdicts:
    store = SqliteVerdicts(databases, incidents, results, events, clock)
    incidents.bind_verdicts(store)
    return store


async def store_result(
    results: ResultsService,
    project_id: str,
    *,
    check_id: str,
    ran_at: datetime,
    fired: bool,
    origin: ResultOrigin = "agent",
    category: ResultCategory = "model_performance",
    subject: str = "churn_scores",
    model: str | None = "churn",
    version: str | None = None,
    not_enough_data: bool = False,
) -> StoredResult:
    record = ResultRecord(
        check_id=check_id,
        origin=origin,
        ran_at=ran_at,
        fired=fired,
        category=category,
        subject=subject,
        model=model,
        detail={},
        source_ref=f"results.jsonl:{check_id}",
        version=version,
        not_enough_data=not_enough_data,
    )
    stored = await results.ingest(project_id, SOURCE, [(f"k{next(_keys)}", record)], ran_at)
    return stored[0]


async def open_incident(
    incidents: IncidentsService,
    project_id: str,
    *,
    subject: str = "churn_scores",
    model: str | None = "churn",
    category: ResultCategory = "model_performance",
    started_at: datetime = T0,
    result_id: str | None = None,
    resolved: bool = False,
) -> str:
    """Opens an incident; `resolved` opens it already resolved (a cleared signal)."""
    draft = SignalDraft(
        kind="fired_result",
        category=category,
        subject=subject,
        model=model,
        ran_at=started_at,
        detail={},
        result_id=result_id,
        cleared=resolved,
    )
    outcome = await incidents.raise_signal(project_id, draft, seen_at=started_at)
    return outcome.incident_id


async def verdict_events(events: SqliteEventLog, project_id: str) -> list[EventRecord]:
    return await events.page(project_id, types=["verdict.recorded"], limit=500)
