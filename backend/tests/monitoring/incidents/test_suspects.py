"""Tests for suspect changes (CI runs, deployments, PRs) and linking to incidents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mlops_agents.agents.pr_sync import PrChange
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.incidents.store import IncidentStore
from mlops_agents.monitoring.incidents.suspects import SuspectLinker
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    Page,
    TrackingQuery,
    TrackingSourceView,
)
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
)


class FakeTracking:
    def __init__(self) -> None:
        self.ci_runs: list[CiRunRow] = []
        self.deployments: list[DeploymentRow] = []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "ci_runs":
            return Page(rows=list(self.ci_runs), total=len(self.ci_runs), truncated=False)
        if q.what == "deployments":
            return Page(rows=list(self.deployments), total=len(self.deployments), truncated=False)
        return Page(rows=[], total=0, truncated=False)

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        return []

    async def create_source(
        self, project_id: str, type: Any, settings: Any, auth: Any
    ) -> TrackingSourceView:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        pass


@pytest.mark.asyncio
async def test_suspect_linker_sync_observer_and_idempotency(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A deployment 1 hour before an incident is linked once across two syncs, one outside window is not, and change arriving after open is linked too."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
    )
    tracking = FakeTracking()
    linker = SuspectLinker(
        databases=databases,
        incidents=service,
        tracking=tracking,
        events=event_log,
        clock=fake_clock,
    )

    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # 1. Open incident at t0
    d = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model="churn",
        ran_at=t0,
        detail={},
    )
    outcome = await service.raise_signal(project_id, d, seen_at=t0)
    inc_id = outcome.incident_id

    # 2. Add two deployments to tracking:
    # - dep_1: 1 hour before incident (within 24h window)
    # - dep_outside: 30 hours before incident (outside 24h window)
    t_1h_before = t0 - timedelta(hours=1)
    t_30h_before = t0 - timedelta(hours=30)
    dep_1 = DeploymentRow(
        source_id="gh",
        model="churn",
        alias="prod",
        from_version="v1",
        to_version="v2",
        seen_at=t_1h_before,
    )
    dep_outside = DeploymentRow(
        source_id="gh",
        model="churn",
        alias="prod",
        from_version="v0",
        to_version="v1",
        seen_at=t_30h_before,
    )
    tracking.deployments = [dep_1, dep_outside]

    # Sync 1
    await linker.synced(project_id)

    # Verify dep_1 is linked, dep_outside is not
    events_1 = await event_log.page(project_id, subject_id=inc_id)
    suspect_events_1 = [e for e in events_1 if e.type == "incident.suspect_linked"]
    assert len(suspect_events_1) == 1
    assert suspect_events_1[0].payload["kind"] == "deployment"
    assert "v2" in suspect_events_1[0].payload["ref"]

    # Sync 2 -> must be idempotent (still linked once across two syncs)
    await linker.synced(project_id)
    events_2 = await event_log.page(project_id, subject_id=inc_id)
    suspect_events_2 = [e for e in events_2 if e.type == "incident.suspect_linked"]
    assert len(suspect_events_2) == 1

    # 3. A change arriving after the incident opened is linked too
    t_change_after = t0 + timedelta(minutes=15)
    ci_after = CiRunRow(
        source_id="gh",
        key="run_999",
        workflow="nightly.yml",
        status="succeeded",
        start=t_change_after,
        duration_s=120.0,
        commit="abc1234",
        url="https://github.com/run/999",
    )
    tracking.ci_runs = [ci_after]

    await linker.synced(project_id)

    events_3 = await event_log.page(project_id, subject_id=inc_id)
    suspect_events_3 = [e for e in events_3 if e.type == "incident.suspect_linked"]
    assert len(suspect_events_3) == 2
    ci_linked = [e for e in suspect_events_3 if e.payload["kind"] == "ci_run"]
    assert len(ci_linked) == 1
    assert ci_linked[0].payload["ref"] == "gh:run_999"


@pytest.mark.asyncio
async def test_suspect_linker_pr_observer_updates_prs(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Merged PRs arriving via PrObserver are linked as suspects and populate IncidentView.prs."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
    )
    tracking = FakeTracking()
    linker = SuspectLinker(
        databases=databases,
        incidents=service,
        tracking=tracking,
        events=event_log,
        clock=fake_clock,
    )

    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # 1. Open incident
    d = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model=None,
        ran_at=t0,
        detail={},
    )
    outcome = await service.raise_signal(project_id, d, seen_at=t0)
    inc_id = outcome.incident_id
    inc_view = await service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.prs == []

    # 2. Non-merged PR arrives -> not linked
    pr_open = PrChange(
        number=101,
        state="open",
        merged_at=None,
        head_branch="feature",
        agent="agent_coder",
        files=["train.py"],
    )
    await linker.changed(project_id, pr_open)
    inc_view = await service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.prs == []

    # 3. Merged PR arrives -> linked as suspect and updates prs
    pr_merged = PrChange(
        number=42,
        state="merged",
        merged_at=t0 - timedelta(minutes=20),
        head_branch="feature",
        agent="agent_coder",
        files=["train.py"],
    )
    await linker.changed(project_id, pr_merged)

    inc_view = await service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.prs == [42]

    events = await event_log.page(project_id, subject_id=inc_id)
    pr_suspects = [
        e for e in events if e.type == "incident.suspect_linked" and e.payload["kind"] == "agent_pr"
    ]
    assert len(pr_suspects) == 1
    assert pr_suspects[0].payload["ref"] == "42"


@pytest.mark.asyncio
async def test_suspects_linked_on_incident_open(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Suspects present in recent_changes within window before started_at are linked on open, populating prs."""
    store = IncidentStore(databases)
    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # Pre-populate recent_changes:
    # 1. Merged PR 1 hour before
    await store.insert_recent_change(
        project_id,
        kind="agent_pr",
        ref="77",
        at=t0 - timedelta(hours=1),
        detail={"number": 77},
    )
    # 2. CI run 30 minutes before
    await store.insert_recent_change(
        project_id,
        kind="ci_run",
        ref="gh:run_10",
        at=t0 - timedelta(minutes=30),
        detail={"workflow": "deploy.yml"},
    )
    # 3. Deployment 28 hours before (outside 24h window)
    await store.insert_recent_change(
        project_id,
        kind="deployment",
        ref="gh:churn:prod:v1",
        at=t0 - timedelta(hours=28),
        detail={"to_version": "v1"},
    )

    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
    )

    # Open incident at t0
    d = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model=None,
        ran_at=t0,
        detail={},
    )
    outcome = await service.raise_signal(project_id, d, seen_at=t0)
    inc_id = outcome.incident_id

    # View should have prs populated on open!
    inc_view = await service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.prs == [77]

    # Events should include suspect_linked for PR 77 and CI run 10, but NOT the 28h old deployment
    events = await event_log.page(project_id, subject_id=inc_id)
    suspect_events = [e for e in events if e.type == "incident.suspect_linked"]
    assert len(suspect_events) == 2
    linked_kinds = {e.payload["kind"] for e in suspect_events}
    assert linked_kinds == {"agent_pr", "ci_run"}


@pytest.mark.asyncio
async def test_baseline_deployment_is_never_stored_as_recent_change_or_suspect(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A baseline deployment row is never stored as a recent change, so no incident gets it as a suspect."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
    )
    tracking = FakeTracking()
    linker = SuspectLinker(
        databases=databases,
        incidents=service,
        tracking=tracking,
        events=event_log,
        clock=fake_clock,
    )

    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # Open incident at t0
    d = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model="churn",
        ran_at=t0,
        detail={},
    )
    outcome = await service.raise_signal(project_id, d, seen_at=t0)
    inc_id = outcome.incident_id

    # Baseline deployment within 24h window
    dep_baseline = DeploymentRow(
        source_id="gh",
        model="churn",
        alias="prod",
        from_version=None,
        to_version="v1",
        seen_at=t0 - timedelta(hours=1),
        baseline=True,
    )
    tracking.deployments = [dep_baseline]

    await linker.synced(project_id)

    # Check store: recent_changes must NOT contain dep_baseline
    store = IncidentStore(databases)
    recent = await store.list_recent_changes(project_id)
    assert len(recent) == 0

    # No suspect linked
    events = await event_log.page(project_id, subject_id=inc_id)
    suspect_events = [e for e in events if e.type == "incident.suspect_linked"]
    assert len(suspect_events) == 0
