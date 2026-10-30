"""Tests for severity changes, observer resilience, and bound verdicts."""

from __future__ import annotations

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    IncidentObserver,
    IncidentPatch,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.verdicts.contract import VerdictDraft
from mlops_agents.storage.databases import Databases
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
    FakeVerdicts,
)


class RaisingObserver(IncidentObserver):
    async def changed(self, project_id: str, change: IncidentChange) -> None:
        raise RuntimeError("Observer intentionally failed!")


class ChangeRecorder(IncidentObserver):
    def __init__(self) -> None:
        self.changes: list[IncidentChange] = []

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        self.changes.append(change)


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


@pytest.mark.asyncio
async def test_subtype_change_emits_cause_and_severity_raised_when_severity_rises(
    service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    recorder = ChangeRecorder()
    service.add_observer(recorder)

    # Initial severity policy: default is medium, but subtype "bad_deploy" is critical
    fake_severity.default_severity = "medium"
    fake_severity.decisions["bad_deploy"] = "critical"
    fake_severity.decisions["normal_op"] = "medium"

    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="deploy_wf",
        model=None,
        ran_at=now,
        detail={},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)
    assert outcome.severity == "medium"
    recorder.changes.clear()

    # Subtype change to "bad_deploy" raises severity to critical:
    # should emit both "cause" and "severity_raised"
    await service.update(
        project_id,
        outcome.incident_id,
        agent="test_agent",
        patch=IncidentPatch(subtype="bad_deploy"),
    )

    kinds = [c.kind for c in recorder.changes]
    assert "cause" in kinds
    assert "severity_raised" in kinds
    sev_change = next(c for c in recorder.changes if c.kind == "severity_raised")
    assert sev_change.before == "medium"
    assert sev_change.after == "critical"

    inc = await service.get(project_id, outcome.incident_id)
    assert inc is not None
    assert inc.severity == "critical"


@pytest.mark.asyncio
async def test_subtype_change_emits_only_cause_when_severity_does_not_rise(
    service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    recorder = ChangeRecorder()
    service.add_observer(recorder)

    fake_severity.default_severity = "medium"
    fake_severity.decisions["some_sub"] = "medium"

    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="deploy_wf",
        model=None,
        ran_at=now,
        detail={},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)
    recorder.changes.clear()

    # Subtype change to "some_sub" keeps severity at medium:
    # should emit only "cause"
    await service.update(
        project_id,
        outcome.incident_id,
        agent="test_agent",
        patch=IncidentPatch(subtype="some_sub"),
    )

    kinds = [c.kind for c in recorder.changes]
    assert kinds == ["cause"]


@pytest.mark.asyncio
async def test_failing_observer_does_not_stop_next_nor_fail_raise_signal(
    service: IncidentsService,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    bad_obs = RaisingObserver()
    good_obs = ChangeRecorder()

    service.add_observer(bad_obs)
    service.add_observer(good_obs)

    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="customers",
        model=None,
        ran_at=now,
        detail={},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)
    assert outcome.change == "opened"

    # Good observer still got notified
    assert len(good_obs.changes) == 1
    assert good_obs.changes[0].kind == "opened"


@pytest.mark.asyncio
async def test_bind_verdicts_populates_verdict_field(
    service: IncidentsService,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="customers",
        model=None,
        ran_at=now,
        detail={},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)

    # Unbound: verdict is None
    inc_unbound = await service.get(project_id, outcome.incident_id)
    assert inc_unbound is not None
    assert inc_unbound.verdict is None

    # Bind verdicts and record one
    verdicts = FakeVerdicts()
    await verdicts.record(
        project_id,
        outcome.incident_id,
        VerdictDraft(value="real_issue"),
        by="user",
    )
    service.bind_verdicts(verdicts)

    # Bound: verdict is populated
    inc_bound = await service.get(project_id, outcome.incident_id)
    assert inc_bound is not None
    assert inc_bound.verdict is not None
    assert inc_bound.verdict.value == "real_issue"
