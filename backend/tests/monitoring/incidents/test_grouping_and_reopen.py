"""Tests for incident grouping, downstream reachability, quiet period and reopen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    IncidentObserver,
    IncidentPatch,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.grouping import is_downstream, parse_subject_from_cause_key
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
)
from tests.proposals.samples import handoff as sample_handoff_dict


class FakeHandoffs:
    def __init__(self, payload: HandoffPayload | None = None) -> None:
        self._payload = payload

    def set_payload(self, payload: HandoffPayload | None) -> None:
        self._payload = payload

    def add_observer(self, observer: object) -> None:
        pass

    async def current(self, project_id: str) -> HandoffView | None:
        if self._payload is None:
            return None
        return HandoffView(
            proposal_id="prop_test",
            version=1,
            approved=datetime.now(UTC),
            payload=self._payload,
        )

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        self._payload = payload
        return 1


class RecordingObserver(IncidentObserver):
    def __init__(self) -> None:
        self.changes: list[IncidentChange] = []

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        self.changes.append(change)


def test_parse_subject_from_cause_key() -> None:
    assert parse_subject_from_cause_key("5:table:m:churn") == "table"
    assert parse_subject_from_cause_key("14:nested:subject:none") == "nested:subject"
    assert parse_subject_from_cause_key("invalid") == ""


@pytest.mark.asyncio
async def test_grouping_same_key_inside_and_outside_window(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Same cause key inside grouping_window_minutes attaches, outside opens new incident."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # Signal 1 at t0 -> opens incident 1
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model="churn",
        ran_at=t0,
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=t0)
    assert out1.change == "opened"

    # Signal 2 at t0 + 30m (inside default 60m window) -> attaches to incident 1
    d2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model="churn",
        ran_at=t0 + timedelta(minutes=30),
        detail={},
    )
    out2 = await service.raise_signal(project_id, d2, seen_at=t0 + timedelta(minutes=30))
    assert out2.incident_id == out1.incident_id
    assert out2.change in ("attached", "quiet")

    # Signal 3 at t0 + 95m (65m after latest signal at t0 + 30m, outside 60m window) -> opens incident 2
    d3 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model="churn",
        ran_at=t0 + timedelta(minutes=95),
        detail={},
    )
    out3 = await service.raise_signal(project_id, d3, seen_at=t0 + timedelta(minutes=95))
    assert out3.change == "opened"
    assert out3.incident_id != out1.incident_id


@pytest.mark.asyncio
async def test_grouping_downstream_reachability_and_cycle_handling(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A column subject downstream of a schema-change incident joins it through cycles without looping."""
    # Build payload with cycle: stage_schema -> stage_features -> stage_train -> stage_schema
    data = sample_handoff_dict()
    data["pipeline"] = [
        {
            "name": "stage_schema",
            "entry": "s1.py",
            "inputs": ["raw_schema"],
            "outputs": ["clean_data"],
        },
        {
            "name": "stage_features",
            "entry": "s2.py",
            "inputs": ["clean_data"],
            "outputs": ["features", "column_subject"],
        },
        {
            "name": "stage_train",
            "entry": "s3.py",
            "inputs": ["features"],
            "outputs": ["model", "raw_schema"],
        },
    ]
    data["edges"] = [
        {"from_stage": "stage_schema", "to_stage": "stage_features"},
        {"from_stage": "stage_features", "to_stage": "stage_train"},
        {"from_stage": "stage_train", "to_stage": "stage_schema"},  # cycle!
    ]
    payload = HandoffPayload.model_validate(data)

    # Unit check is_downstream directly
    assert is_downstream("raw_schema", "column_subject", payload) is True
    assert is_downstream("column_subject", "raw_schema", payload) is True  # reachable via cycle
    assert is_downstream("unmapped", "column_subject", payload) is False
    assert is_downstream("raw_schema", "unmapped", payload) is False
    assert is_downstream("raw_schema", "column_subject", None) is False

    handoffs = FakeHandoffs(payload)
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Incident opened on schema subject
    d_schema = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="raw_schema",
        model=None,
        ran_at=t0,
        detail={},
    )
    out_schema = await service.raise_signal(project_id, d_schema, seen_at=t0)
    assert out_schema.change == "opened"

    # 2. Downstream column subject arrives (different subject, different cause key)
    d_col = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="column_subject",
        model=None,
        ran_at=t0 + timedelta(minutes=10),
        detail={},
    )
    out_col = await service.raise_signal(project_id, d_col, seen_at=t0 + timedelta(minutes=10))
    # Must join the upstream incident!
    assert out_col.incident_id == out_schema.incident_id

    # 3. An unmapped subject groups by key only -> does not join
    d_unmapped = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="unmapped_table",
        model=None,
        ran_at=t0 + timedelta(minutes=15),
        detail={},
    )
    out_unmapped = await service.raise_signal(
        project_id, d_unmapped, seen_at=t0 + timedelta(minutes=15)
    )
    assert out_unmapped.incident_id != out_schema.incident_id
    assert out_unmapped.change == "opened"


@pytest.mark.asyncio
async def test_grouping_oldest_open_incident_wins(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """When several candidates match downstream, the oldest open incident wins."""
    data = sample_handoff_dict()
    data["pipeline"] = [
        {"name": "stage_a", "entry": "a.py", "inputs": ["sub_a"], "outputs": ["out_a"]},
        {"name": "stage_b", "entry": "b.py", "inputs": ["sub_b"], "outputs": ["out_b"]},
        {
            "name": "stage_down",
            "entry": "d.py",
            "inputs": ["out_a", "out_b"],
            "outputs": ["target"],
        },
    ]
    data["edges"] = [
        {"from_stage": "stage_a", "to_stage": "stage_down"},
        {"from_stage": "stage_b", "to_stage": "stage_down"},
    ]
    payload = HandoffPayload.model_validate(data)

    handoffs = FakeHandoffs(payload)
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # Incident 1 (older, started at t0)
    out1 = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="sub_a",
            model=None,
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )

    # Incident 2 (newer, started at t0 + 10m)
    out2 = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="sub_b",
            model=None,
            ran_at=t0 + timedelta(minutes=10),
            detail={},
        ),
        seen_at=t0 + timedelta(minutes=10),
    )

    assert out1.incident_id != out2.incident_id

    # Draft matching target is downstream of BOTH stage_a (out1) and stage_b (out2)
    out_target = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="target",
            model=None,
            ran_at=t0 + timedelta(minutes=20),
            detail={},
        ),
        seen_at=t0 + timedelta(minutes=20),
    )

    # Oldest incident (out1) must win!
    assert out_target.incident_id == out1.incident_id


@pytest.mark.asyncio
async def test_quiet_attach_vs_severity_rise(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A quiet attach notifies no observer, a severity rise does."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    obs = RecordingObserver()
    service.add_observer(obs)

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Open incident with medium severity
    out1 = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="orders",
            model=None,
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    assert out1.change == "opened"
    assert len(obs.changes) == 1
    assert obs.changes[0].kind == "opened"

    # 2. Quiet attach: severity does not rise
    out2 = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="orders",
            model=None,
            ran_at=t0 + timedelta(minutes=10),
            detail={},
        ),
        seen_at=t0 + timedelta(minutes=10),
    )
    assert out2.change == "quiet"
    # Observer was NOT called again for quiet attach!
    assert len(obs.changes) == 1

    # 3. Severity rise: severity policy now decides critical
    severity.decisions["data_quality"] = "critical"
    out3 = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="orders",
            model=None,
            ran_at=t0 + timedelta(minutes=20),
            detail={},
        ),
        seen_at=t0 + timedelta(minutes=20),
    )
    assert out3.change == "attached"
    assert out3.severity == "critical"
    # Observer got severity_raised
    assert len(obs.changes) == 2
    assert obs.changes[1].kind == "severity_raised"
    assert obs.changes[1].before == "medium"
    assert obs.changes[1].after == "critical"


@pytest.mark.asyncio
async def test_reopen_inside_window_keeps_id(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Reopen inside reopen_window_hours keeps ID, increments reopened_count and emits change."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )
    obs = RecordingObserver()
    service.add_observer(obs)

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Open incident
    d = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model=None,
        ran_at=t0,
        detail={},
    )
    out1 = await service.raise_signal(project_id, d, seen_at=t0)
    inc_id = out1.incident_id

    # 2. Resolve incident at t0 + 1h
    fake_clock.advance(3600)
    await service.update(
        project_id,
        inc_id,
        agent="user",
        patch=IncidentPatch(status="resolved"),
    )
    inc_view = await service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.status == "resolved"

    # 3. New signal arrives 3 hours after resolution (inside 24h window)
    t_new = t0 + timedelta(hours=4)
    d_reopen = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="orders",
        model=None,
        ran_at=t_new,
        detail={},
    )
    out_reopen = await service.raise_signal(project_id, d_reopen, seen_at=t_new)
    assert out_reopen.change == "reopened"
    assert out_reopen.incident_id == inc_id

    view_reopened = await service.get(project_id, inc_id)
    assert view_reopened is not None
    assert view_reopened.status == "reopened"
    assert view_reopened.reopened_count == 1
    assert view_reopened.resolved_at is None

    # Verify event recorded
    events_list = await event_log.page(project_id, subject_id=inc_id)
    event_types = [e.type for e in events_list]
    assert "incident.reopened" in event_types

    # Observer notified of reopened
    reopened_changes = [c for c in obs.changes if c.kind == "reopened"]
    assert len(reopened_changes) == 1


@pytest.mark.asyncio
async def test_reopen_outside_window_and_closed_never_reopened(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Outside reopen_window_hours opens a new incident, and a closed incident is never reopened."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Open and resolve incident 1
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_out",
        model=None,
        ran_at=t0,
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=t0)
    await service.update(
        project_id,
        out1.incident_id,
        agent="user",
        patch=IncidentPatch(status="resolved"),
    )

    # Signal 28 hours later (outside 24h reopen window) -> opens new incident!
    t_late = t0 + timedelta(hours=28)
    d_late = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_out",
        model=None,
        ran_at=t_late,
        detail={},
    )
    out_late = await service.raise_signal(project_id, d_late, seen_at=t_late)
    assert out_late.change == "opened"
    assert out_late.incident_id != out1.incident_id

    # 2. Close the new incident
    await service.close(project_id, out_late.incident_id, by="user")
    view_closed = await service.get(project_id, out_late.incident_id)
    assert view_closed is not None
    assert view_closed.status == "closed"

    # Signal 10 minutes after close -> MUST open new incident (closed is never reopened)
    t_after_close = t_late + timedelta(minutes=10)
    d_after_close = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_out",
        model=None,
        ran_at=t_after_close,
        detail={},
    )
    out_after_close = await service.raise_signal(project_id, d_after_close, seen_at=t_after_close)
    assert out_after_close.change == "opened"
    assert out_after_close.incident_id != out_late.incident_id
    assert out_after_close.incident_id != out1.incident_id
