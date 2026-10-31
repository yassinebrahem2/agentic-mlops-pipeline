"""Real stores on a temp data folder for the stats tests, and helpers that seed their rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.incidents.contract import MediumSeverity, SignalDraft
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.contract import (
    ResultCategory,
    ResultOrigin,
    ResultRecord,
    StoredResult,
)
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.baseline import (
    BaselineIncidentObserver,
    BaselineResultsObserver,
    SqliteBaseline,
)
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from mlops_agents.providers.models import ModelEntry
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.stats.service import StatsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import (
    approvals,
    events,
    hook_runs,
    messages,
    sessions,
    tasks,
    turns,
)
from tests.conftest import memory_project_secrets
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.monitoring.verdicts.conftest import SOURCE
from tests.providers.fake_clock import FakeClock

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
_keys = count()


def ago(*, days: float = 0, hours: float = 0, minutes: float = 0, seconds: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


class FakeModels:
    """The model list: an address absent from `prices` is a model that does not exist."""

    def __init__(self) -> None:
        self.prices: dict[str, ModelPrice | None] = {}

    async def get_model(self, address: str) -> ModelEntry | None:
        if address not in self.prices:
            return None
        return ModelEntry(
            address=address,
            provider_id="prov_1",
            provider_name="fake",
            wire_model_id=address.split(":", 1)[-1],
            context_window=None,
            tool_calling=True,
            streaming=True,
            availability="verified",
            availability_detail=None,
            verified_at=None,
            price=self.prices[address],
        )


@pytest.fixture
def project_id() -> str:
    return "prj_teststats"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def results(databases: Databases, event_log: SqliteEventLog) -> ResultsService:
    return ResultsService(databases, FakeHandoffs(), FakeConnections(), event_log)


@pytest.fixture
def incidents(
    databases: Databases, event_log: SqliteEventLog, results: ResultsService, clock: FakeClock
) -> IncidentsService:
    return IncidentsService(
        databases=databases,
        events=event_log,
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
    event_log: SqliteEventLog,
    clock: FakeClock,
) -> SqliteVerdicts:
    store = SqliteVerdicts(databases, incidents, results, event_log, clock)
    incidents.bind_verdicts(store)
    return store


@pytest.fixture
def baseline(
    databases: Databases,
    results: ResultsService,
    incidents: IncidentsService,
    event_log: SqliteEventLog,
    clock: FakeClock,
) -> SqliteBaseline:
    store = SqliteBaseline(databases, results, incidents, event_log, clock)
    results.add_observer(BaselineResultsObserver(store))
    incidents.add_observer(BaselineIncidentObserver(store))
    return store


@pytest.fixture
def hooks(databases: Databases, event_log: SqliteEventLog, clock: FakeClock) -> HooksService:
    return HooksService(databases, memory_project_secrets(databases), event_log, clock)


@pytest.fixture
def models() -> FakeModels:
    return FakeModels()


@pytest.fixture
def stats(
    databases: Databases,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    baseline: SqliteBaseline,
    results: ResultsService,
    hooks: HooksService,
    models: FakeModels,
) -> StatsService:
    return StatsService(
        databases=databases,
        incidents=incidents,
        verdicts=verdicts,
        baseline=baseline,
        results=results,
        hooks=hooks,
        models=models,
    )


async def raise_on(
    incidents: IncidentsService,
    project_id: str,
    *,
    subject: str,
    started_at: datetime,
    detected_at: datetime | None = None,
    category: ResultCategory = "model_performance",
    model: str | None = "churn",
) -> str:
    """Raises a signal: opens an incident, or attaches to the live one on the same key."""
    draft = SignalDraft(
        kind="fired_result",
        category=category,
        subject=subject,
        model=model,
        ran_at=started_at,
        detail={},
    )
    outcome = await incidents.raise_signal(project_id, draft, seen_at=detected_at or started_at)
    return outcome.incident_id


def result_record(
    ran_at: datetime,
    *,
    subject: str,
    origin: ResultOrigin = "team",
    fired: bool = True,
    check_id: str = "team_recall",
    value: float | None = None,
    not_enough_data: bool = False,
    model: str | None = "churn",
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin=origin,
        ran_at=ran_at,
        fired=fired,
        value=value,
        category="model_performance",
        subject=subject,
        model=model,
        detail={},
        source_ref=f"results.jsonl:{check_id}",
        not_enough_data=not_enough_data,
    )


async def store(results: ResultsService, project_id: str, record: ResultRecord) -> StoredResult:
    """Stores one result and delivers it to the observers, as a collection run does."""
    [stored] = await results.ingest(project_id, SOURCE, [(f"st{next(_keys)}", record)], NOW)
    await results.deliver_undelivered(project_id, NOW)
    return stored


async def team_fire(
    results: ResultsService, project_id: str, ran_at: datetime, *, subject: str
) -> StoredResult:
    return await store(results, project_id, result_record(ran_at, subject=subject))


async def _insert(
    databases: Databases, project_id: str, table: Any, rows: list[dict[str, Any]]
) -> None:
    """Inserts `rows`, after the session, turn and message they point to (a quiet turn and an
    empty message long before every window)."""
    long_ago = "2020-01-01T00:00:00.000Z"
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        for parent, values in (
            (sessions, {"id": "ses_1", "agent": "engineer", "created_at": long_ago}),
            (
                turns,
                {
                    "id": "trn_1",
                    "session_id": "ses_1",
                    "trigger_message_id": "msg_1",
                    "status": "done",
                    "created_at": long_ago,
                },
            ),
            (
                messages,
                {
                    "id": "msg_1",
                    "session_id": "ses_1",
                    "author_kind": "user",
                    "author_name": "user",
                    "parts": "[]",
                    "status": "complete",
                    "created_at": long_ago,
                },
            ),
        ):
            await conn.execute(sqlite_insert(parent).values(**values).on_conflict_do_nothing())
        await conn.execute(insert(table), rows)


async def add_hook_runs(
    databases: Databases, project_id: str, runs: list[tuple[str, str, datetime]]
) -> None:
    """Each run is (kind, basis, started)."""
    await _insert(
        databases,
        project_id,
        hook_runs,
        [
            {
                "id": new_id("hok"),
                "hook_id": "hok_1",
                "kind": kind,
                "reason": "test",
                "basis": basis,
                "evidence": "[]",
                "started": to_iso(started),
            }
            for kind, basis, started in runs
        ],
    )


async def add_approvals(
    databases: Databases, project_id: str, rows: list[tuple[str, datetime]]
) -> None:
    """Each approval is (status, created_at)."""
    await _insert(
        databases,
        project_id,
        approvals,
        [
            {
                "id": new_id("apr"),
                "turn_id": "trn_1",
                "session_id": "ses_1",
                "message_id": "msg_1",
                "agent": "engineer",
                "preview": "{}",
                "status": status,
                "created_at": to_iso(created_at),
                "kind": "tool",
                "tool_call_id": "call_1",
                "tool": "Bash",
                "risk_class": "execute",
            }
            for status, created_at in rows
        ],
    )


async def add_tasks(
    databases: Databases,
    project_id: str,
    rows: list[tuple[str, int | None, datetime, datetime | None]],
) -> None:
    """Each task is (status, pr_number, created_at, ended_at)."""
    await _insert(
        databases,
        project_id,
        tasks,
        [
            {
                "id": new_id("tsk"),
                "session_id": "ses_1",
                "agent": "engineer",
                "title": "fix",
                "branch": "agent/fix",
                "worktree": "wt",
                "base_ref": "main",
                "base_sha": "abc",
                "status": status,
                "pr_number": pr_number,
                "created_at": to_iso(created_at),
                "ended_at": to_iso(ended_at) if ended_at is not None else None,
            }
            for status, pr_number, created_at, ended_at in rows
        ],
    )


async def add_turns(
    databases: Databases, project_id: str, rows: list[tuple[str, datetime]]
) -> None:
    """Each turn is (status, created_at)."""
    await _insert(
        databases,
        project_id,
        turns,
        [
            {
                "id": new_id("trn"),
                "session_id": "ses_1",
                "trigger_message_id": "msg_1",
                "status": status,
                "created_at": to_iso(created_at),
            }
            for status, created_at in rows
        ],
    )


async def add_messages(
    databases: Databases, project_id: str, rows: list[tuple[list[dict[str, Any]], datetime]]
) -> None:
    """Each message is (parts, created_at)."""
    await _insert(
        databases,
        project_id,
        messages,
        [
            {
                "id": new_id("msg"),
                "session_id": "ses_1",
                "author_kind": "agent",
                "author_name": "engineer",
                "parts": json.dumps(parts),
                "status": "complete",
                "created_at": to_iso(created_at),
            }
            for parts, created_at in rows
        ],
    )


async def add_events(
    databases: Databases, project_id: str, rows: list[tuple[str, datetime]]
) -> None:
    """Each event is (type, ts)."""
    await _insert(
        databases,
        project_id,
        events,
        [
            {
                "id": new_id("evt"),
                "ts": to_iso(ts),
                "source": "sandbox",
                "type": event_type,
                "payload": "{}",
            }
            for event_type, ts in rows
        ],
    )
