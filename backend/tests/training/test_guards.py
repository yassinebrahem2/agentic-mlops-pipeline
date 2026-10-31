"""Tests for training guards (Task 2 of Stage 5 Training Plan, #487)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import (
    AgentWaker,
    HoldScope,
    Incidents,
    IncidentStatus,
    IncidentView,
    SignalView,
)
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page
from mlops_agents.training.contract import GuardResult, TrainingWindow
from mlops_agents.training.fixes import SqliteFixes
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock


class FakeIncidents:
    def __init__(
        self,
        incidents: list[IncidentView] | None = None,
        signals_map: dict[str, list[SignalView]] | None = None,
        holds_list: list[HoldScope] | None = None,
    ) -> None:
        self._incidents = incidents or []
        self._signals = signals_map or {}
        self._holds = holds_list or []

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        for inc in self._incidents:
            if inc.id == incident_id:
                return inc
        return None

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        matched = [inc for inc in self._incidents if status is None or inc.status in status]
        return Page(rows=matched[offset : offset + limit], total=len(matched), truncated=False)

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]:
        return self._signals.get(incident_id, [])

    async def holds(self, project_id: str, model: str | None) -> list[HoldScope]:
        return list(self._holds)


class FakeHandoffs:
    def __init__(self, current_handoff: HandoffView | None = None) -> None:
        self._current = current_handoff

    def set_current(self, current_handoff: HandoffView | None) -> None:
        self._current = current_handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current


class FakeAgentWaker:
    async def wake(self, *args: Any, **kwargs: Any) -> None:
        pass


def make_fixes(
    databases: Databases,
    incidents: FakeIncidents,
    clock: FakeClock,
    handoffs: FakeHandoffs | None = None,
) -> SqliteFixes:
    events = SqliteEventLog(databases)
    effective_handoffs = handoffs if handoffs is not None else FakeHandoffs()
    return SqliteFixes(
        databases=databases,
        incidents=cast(Incidents, incidents),
        waker=cast(AgentWaker, FakeAgentWaker()),
        events=events,
        clock=clock,
        handoffs=cast(Handoffs, effective_handoffs),
    )


def make_handoff_view(
    model_name: str = "cancel",
    maturity_days: int = 7,
    training_table: str = "bookings_train",
) -> HandoffView:
    payload_dict = handoff()
    payload_dict["models"][0]["name"] = model_name
    payload_dict["models"][0]["profile"]["label_maturity_days"] = maturity_days
    payload_dict["data_roles"].append(
        {
            "connection": "hotel",
            "table": training_table,
            "role": "training",
            "id_column": "booking_id",
            "time_column": "created_at",
        }
    )
    payload = HandoffPayload.model_validate(payload_dict)
    return HandoffView(
        proposal_id="prop_guards",
        version=1,
        approved=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


def make_incident(
    incident_id: str,
    status: IncidentStatus = "open",
    category: str = "data_quality",
    started_at: datetime | None = None,
) -> IncidentView:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    st = started_at or (now - timedelta(days=2))
    return IncidentView(
        id=incident_id,
        category=category,  # pyright: ignore[reportArgumentType]
        subtype="null_spike",
        severity="medium",
        status=status,
        cause=f"Data quality issue on {incident_id}",
        cause_key=f"dq:{incident_id}",
        model="cancel",
        signal_ids=[f"sig_{incident_id}"],
        prs=[],
        issues=[],
        held=False,
        merged_into=None,
        started_at=st,
        detected_at=st,
    )


def make_signal(
    signal_id: str,
    incident_id: str,
    subject: str = "bookings_train",
) -> SignalView:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    return SignalView(
        id=signal_id,
        incident_id=incident_id,
        kind="fired_result",
        category="data_quality",
        subject=subject,
        model="cancel",
        ran_at=now,
        detail={"error": "anomaly"},
        seen_at=now,
        cause_key=f"dq:{subject}",
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.mark.asyncio
async def test_guards_all_pass(databases: Databases, fake_clock: FakeClock) -> None:
    """When labels are mature, no holds exist, and no open DQ incidents, guards pass."""
    handoff_view = make_handoff_view("cancel", maturity_days=7)
    handoffs = FakeHandoffs(handoff_view)
    incidents = FakeIncidents()
    fixes = make_fixes(databases, incidents, fake_clock, handoffs=handoffs)

    # now is 2026-10-31 12:00:00 UTC. 7 days maturity -> cutoff is 2026-10-24 12:00:00 UTC.
    window = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
    )

    result = await fixes.guards("prj_test", "cancel", window)
    assert isinstance(result, GuardResult)
    assert result.ok is True
    assert result.refusals == []


@pytest.mark.asyncio
async def test_guards_data_held(databases: Databases, fake_clock: FakeClock) -> None:
    """A hold overlapping training window on a training table refuses; holds outside or on other tables pass."""
    handoff_view = make_handoff_view("cancel")
    handoffs = FakeHandoffs(handoff_view)

    window = TrainingWindow(
        start=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
    )

    # 1. Matching hold overlapping window on bookings_train
    hold_overlap = HoldScope(
        tables=["bookings_train"],
        start=datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
    )
    incidents = FakeIncidents(holds_list=[hold_overlap])
    fixes = make_fixes(databases, incidents, fake_clock, handoffs=handoffs)

    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert len(result.refusals) == 1
    assert result.refusals[0].code == "data_held"

    # 2. Hold with empty tables (entire model) overlapping window -> refuses
    hold_all = HoldScope(
        tables=[],
        start=datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
    )
    fixes = make_fixes(
        databases, FakeIncidents(holds_list=[hold_all]), fake_clock, handoffs=handoffs
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert result.refusals[0].code == "data_held"

    # 3. Hold ending before the window (hold.end <= window.start) -> passes
    hold_before = HoldScope(
        tables=["bookings_train"],
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
    )
    fixes = make_fixes(
        databases, FakeIncidents(holds_list=[hold_before]), fake_clock, handoffs=handoffs
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True
    assert result.refusals == []

    # 4. Hold starting after window end (hold.start >= window.end) -> passes
    hold_after = HoldScope(
        tables=["bookings_train"],
        start=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 25, 0, 0, 0, tzinfo=UTC),
    )
    fixes = make_fixes(
        databases, FakeIncidents(holds_list=[hold_after]), fake_clock, handoffs=handoffs
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True
    assert result.refusals == []

    # 5. Hold on another table not in training_tables -> passes
    hold_other_table = HoldScope(
        tables=["unrelated_table"],
        start=datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
    )
    fixes = make_fixes(
        databases, FakeIncidents(holds_list=[hold_other_table]), fake_clock, handoffs=handoffs
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True
    assert result.refusals == []

    # 6. Open bound hold (None end) overlapping window -> refuses
    hold_open_end = HoldScope(
        tables=["bookings_train"],
        start=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
        end=None,
    )
    fixes = make_fixes(
        databases, FakeIncidents(holds_list=[hold_open_end]), fake_clock, handoffs=handoffs
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert result.refusals[0].code == "data_held"


@pytest.mark.asyncio
async def test_guards_open_data_quality_incident(
    databases: Databases, fake_clock: FakeClock
) -> None:
    """An open DQ incident with signal on training table overlapping window refuses."""
    handoff_view = make_handoff_view("cancel", maturity_days=7)
    handoffs = FakeHandoffs(handoff_view)

    window = TrainingWindow(
        start=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
    )

    # 1. Matching open DQ incident started at 2026-10-15 (< window.end), now is 2026-10-31 (> window.start)
    inc = make_incident("inc_dq_1", status="open", started_at=datetime(2026, 10, 15, tzinfo=UTC))
    sig = make_signal("sig_1", "inc_dq_1", subject="bookings_train")
    incidents = FakeIncidents(incidents=[inc], signals_map={"inc_dq_1": [sig]})

    fixes = make_fixes(databases, incidents, fake_clock, handoffs=handoffs)
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert len(result.refusals) == 1
    assert result.refusals[0].code == "open_data_quality_incident"

    # 2. Resolved incident -> passes
    inc_resolved = make_incident(
        "inc_res", status="resolved", started_at=datetime(2026, 10, 15, tzinfo=UTC)
    )
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_resolved], signals_map={"inc_res": [sig]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True
    assert result.refusals == []

    # 3. Closed incident -> passes
    inc_closed = make_incident(
        "inc_closed", status="closed", started_at=datetime(2026, 10, 15, tzinfo=UTC)
    )
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_closed], signals_map={"inc_closed": [sig]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True

    # 4. Merged incident -> passes
    inc_merged = make_incident(
        "inc_merged", status="merged", started_at=datetime(2026, 10, 15, tzinfo=UTC)
    )
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_merged], signals_map={"inc_merged": [sig]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True

    # 5. Non-data-quality incident (e.g. model_performance) -> passes
    inc_perf = make_incident(
        "inc_perf",
        status="open",
        category="model_performance",
        started_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_perf], signals_map={"inc_perf": [sig]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True

    # 6. Signal subject is on another table -> passes
    sig_other = make_signal("sig_other", "inc_dq_other", subject="unrelated_table")
    inc_other = make_incident(
        "inc_dq_other", status="open", started_at=datetime(2026, 10, 15, tzinfo=UTC)
    )
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_other], signals_map={"inc_dq_other": [sig_other]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True

    # 7. Incident started after window end (incident.started_at >= window.end) -> passes
    inc_late = make_incident(
        "inc_late", status="open", started_at=datetime(2026, 10, 25, tzinfo=UTC)
    )
    sig_late = make_signal("sig_late", "inc_late", subject="bookings_train")
    fixes = make_fixes(
        databases,
        FakeIncidents(incidents=[inc_late], signals_map={"inc_late": [sig_late]}),
        fake_clock,
        handoffs=handoffs,
    )
    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is True


@pytest.mark.asyncio
async def test_guards_open_data_quality_incident_pagination_and_status_filtering(
    databases: Databases, fake_clock: FakeClock
) -> None:
    """The data quality guard filters to active statuses and pages past 100 items."""
    handoff_view = make_handoff_view("cancel", maturity_days=7)
    handoffs = FakeHandoffs(handoff_view)

    window = TrainingWindow(
        start=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC),
    )

    # Create 120 open incidents of other categories (e.g. drift)
    incidents_list: list[IncidentView] = []
    for i in range(120):
        incidents_list.append(
            make_incident(
                f"inc_drift_{i}",
                status="open",
                category="drift",
                started_at=datetime(2026, 10, 15, tzinfo=UTC),
            )
        )

    # At index 121 (page 2), add an open data_quality incident touching bookings_train
    target_inc = make_incident(
        "inc_dq_target",
        status="open",
        category="data_quality",
        started_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    incidents_list.append(target_inc)
    sig = make_signal("sig_dq", "inc_dq_target", subject="bookings_train")

    # Add resolved/closed incidents that status filter should ignore
    incidents_list.append(
        make_incident(
            "inc_dq_closed",
            status="closed",
            category="data_quality",
            started_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
    )

    fake_incidents = FakeIncidents(incidents=incidents_list, signals_map={"inc_dq_target": [sig]})
    fixes = make_fixes(databases, fake_incidents, fake_clock, handoffs=handoffs)

    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert len(result.refusals) == 1
    assert result.refusals[0].code == "open_data_quality_incident"
    assert "inc_dq_target" in result.refusals[0].detail


@pytest.mark.asyncio
async def test_guards_labels_immature(databases: Databases, fake_clock: FakeClock) -> None:
    """Labels immature if window.end > now - label_maturity_days; passes if <= cutoff."""
    # fake_clock.now() is 2026-10-31 12:00:00 UTC.
    # label_maturity_days = 10 -> cutoff is 2026-10-21 12:00:00 UTC.
    handoff_view = make_handoff_view("cancel", maturity_days=10)
    handoffs = FakeHandoffs(handoff_view)
    incidents = FakeIncidents()
    fixes = make_fixes(databases, incidents, fake_clock, handoffs=handoffs)

    # 1. window.end is after cutoff (2026-10-22 > 2026-10-21 12:00:00) -> refuses
    window_immature = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC),
    )
    result = await fixes.guards("prj_test", "cancel", window_immature)
    assert result.ok is False
    assert len(result.refusals) == 1
    assert result.refusals[0].code == "labels_immature"

    # 2. window.end exactly at cutoff (2026-10-21 12:00:00) -> passes
    window_exact = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )
    result = await fixes.guards("prj_test", "cancel", window_exact)
    assert result.ok is True
    assert result.refusals == []

    # 3. window.end before cutoff -> passes
    window_mature = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
    )
    result = await fixes.guards("prj_test", "cancel", window_mature)
    assert result.ok is True
    assert result.refusals == []


@pytest.mark.asyncio
async def test_guards_multiple_refusals(databases: Databases, fake_clock: FakeClock) -> None:
    """Several refusals are all returned in GuardResult.refusals."""
    # fake_clock.now() is 2026-10-31 12:00:00 UTC. 10 days maturity -> cutoff 2026-10-21 12:00:00
    handoff_view = make_handoff_view("cancel", maturity_days=10)
    handoffs = FakeHandoffs(handoff_view)

    # Hold overlapping window
    hold = HoldScope(
        tables=["bookings_train"],
        start=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 25, 0, 0, 0, tzinfo=UTC),
    )
    # Open DQ incident overlapping window
    inc = make_incident(
        "inc_dq_multi", status="open", started_at=datetime(2026, 10, 16, tzinfo=UTC)
    )
    sig = make_signal("sig_multi", "inc_dq_multi", subject="bookings_train")
    incidents = FakeIncidents(
        incidents=[inc], signals_map={"inc_dq_multi": [sig]}, holds_list=[hold]
    )

    fixes = make_fixes(databases, incidents, fake_clock, handoffs=handoffs)

    # Window ends at 2026-10-25 -> later than cutoff (2026-10-21 12:00:00), so immature labels too
    window = TrainingWindow(
        start=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 25, 0, 0, 0, tzinfo=UTC),
    )

    result = await fixes.guards("prj_test", "cancel", window)
    assert result.ok is False
    codes = [r.code for r in result.refusals]
    assert "data_held" in codes
    assert "open_data_quality_incident" in codes
    assert "labels_immature" in codes
    assert len(result.refusals) == 3


@pytest.mark.asyncio
async def test_guards_no_handoff_refuses(databases: Databases, fake_clock: FakeClock) -> None:
    """Missing handoff or unknown model refuses with a clear message rather than passing."""
    incidents = FakeIncidents()

    window = TrainingWindow(
        start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC),
    )

    # 1. handoffs.current returns None
    handoffs_none = FakeHandoffs(None)
    fixes_none = make_fixes(databases, incidents, fake_clock, handoffs=handoffs_none)
    result = await fixes_none.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert len(result.refusals) >= 1
    assert result.refusals[0].code == "labels_immature"
    assert "handoff" in result.refusals[0].detail.lower()

    # 2. handoffs is None on SqliteFixes
    fixes_no_service = make_fixes(databases, incidents, fake_clock, handoffs=None)
    result = await fixes_no_service.guards("prj_test", "cancel", window)
    assert result.ok is False
    assert len(result.refusals) >= 1
    assert result.refusals[0].code == "labels_immature"
    assert "handoff" in result.refusals[0].detail.lower()

    # 3. Model not found in handoff
    handoff_view = make_handoff_view("cancel")
    handoffs_unknown_model = FakeHandoffs(handoff_view)
    fixes_unknown_model = make_fixes(
        databases, incidents, fake_clock, handoffs=handoffs_unknown_model
    )
    result = await fixes_unknown_model.guards("prj_test", "nonexistent_model", window)
    assert result.ok is False
    assert len(result.refusals) >= 1
    assert result.refusals[0].code == "labels_immature"
    assert "nonexistent_model" in result.refusals[0].detail
