"""Test fixtures and fakes for monitoring incidents."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.incidents.contract import (
    IncidentView,
    Severity,
    SeverityDecision,
)
from mlops_agents.monitoring.results.contract import (
    CollectionReport,
    ResultCategory,
    ResultOrigin,
    ResultQuery,
    ResultsObserver,
    StoredResult,
)
from mlops_agents.monitoring.verdicts.contract import MissReport, VerdictDraft, VerdictView
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page
from tests.providers.fake_clock import FakeClock


class FakeResults:
    def __init__(self) -> None:
        self.observers: list[ResultsObserver] = []
        self.latest_results: list[StoredResult] = []

    def add_observer(self, observer: ResultsObserver) -> None:
        self.observers.append(observer)

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]:
        return [r for r in self.latest_results if r.subject == subject and r.model == model]

    async def ingest(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, Any]],
        seen_at: datetime,
    ) -> list[StoredResult]:
        return []

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        return CollectionReport(stored=0, by_source={}, failures={})

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        return Page(rows=[], total=0, truncated=False)


class FakeMaintenance:
    def __init__(self, suspended_scopes: set[str] | None = None) -> None:
        self.suspended_scopes = suspended_scopes or set()

    async def suspended(self, project_id: str, scope: ExpectationKey | str, at: datetime) -> bool:
        if isinstance(scope, str):
            return scope in self.suspended_scopes
        return False


class FakeSeverityPolicy:
    def __init__(self, default_severity: Severity = "medium") -> None:
        self.default_severity: Severity = default_severity
        self.decisions: dict[str, Severity] = {}

    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision:
        if incident.subtype and incident.subtype in self.decisions:
            return SeverityDecision(
                severity=self.decisions[incident.subtype],
                reasons=[f"Rule for subtype {incident.subtype}"],
            )
        if incident.category in self.decisions:
            return SeverityDecision(
                severity=self.decisions[incident.category],
                reasons=[f"Rule for category {incident.category}"],
            )
        return SeverityDecision(severity=self.default_severity, reasons=["Default decision"])


class FakeVerdicts:
    def __init__(self) -> None:
        self.verdicts: dict[str, VerdictView] = {}

    async def get(self, project_id: str, incident_id: str) -> VerdictView | None:
        return self.verdicts.get(incident_id)

    async def record(
        self, project_id: str, incident_id: str, draft: VerdictDraft, by: str
    ) -> VerdictView:
        view = VerdictView(
            incident_id=incident_id,
            value=draft.value,
            right_category=draft.right_category,
            right_subtype=draft.right_subtype,
            source="user",
            note=draft.note,
            created=datetime.now(UTC),
        )
        self.verdicts[incident_id] = view
        return view

    async def confirm_by_labels(self, project_id: str) -> list[VerdictView]:
        return []

    async def report_miss(self, project_id: str, miss: Any) -> None:
        pass

    async def list(self, project_id: str, since: datetime | None = None) -> list[VerdictView]:
        return list(self.verdicts.values())

    async def list_misses(self, project_id: str, since: datetime | None = None) -> list[MissReport]:
        return []


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
def fake_results() -> FakeResults:
    return FakeResults()


@pytest.fixture
def fake_maintenance() -> FakeMaintenance:
    return FakeMaintenance()


@pytest.fixture
def fake_severity() -> FakeSeverityPolicy:
    return FakeSeverityPolicy()


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def project_id() -> str:
    return "prj_testincidents"


def make_stored_result(
    result_id: str,
    *,
    check_id: str = "chk_1",
    origin: ResultOrigin = "agent",
    fired: bool = True,
    ran_at: datetime | None = None,
    seen_at: datetime | None = None,
    category: ResultCategory = "data_quality",
    subject: str = "customers_table",
    model: str | None = "churn",
    detail: dict[str, Any] | None = None,
    source_id: str = "file:local_s3:results/*.json",
    source_ref: str = "results/chk1.json",
    not_enough_data: bool = False,
) -> StoredResult:
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    return StoredResult(
        id=result_id,
        check_id=check_id,
        origin=origin,
        ran_at=ran_at or now,
        seen_at=seen_at or now,
        fired=fired,
        category=category,
        subject=subject,
        model=model,
        detail=detail or {"score": 0.42},
        source_id=source_id,
        source_key=f"k_{result_id}",
        source_ref=source_ref,
        not_enough_data=not_enough_data,
    )
