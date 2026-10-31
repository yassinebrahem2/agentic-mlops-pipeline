"""Tests for incident tools (IncidentUpdate, IncidentMerge, IncidentSplit, IncidentFollowUp)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import RiskClass, ToolContext
from mlops_agents.agents.tools.names import (
    INCIDENT_FOLLOW_UP,
    INCIDENT_MERGE,
    INCIDENT_SPLIT,
    INCIDENT_UPDATE,
)
from mlops_agents.connections.contract import TableRef
from mlops_agents.data.profile import TableProfile
from mlops_agents.data.summaries import DataSummaries
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.incidents.tools import (
    IncidentFollowUp,
    IncidentMerge,
    IncidentSplit,
    IncidentUpdate,
)
from mlops_agents.monitoring.results.contract import StoredResult
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import incident_follow_ups
from tests.agents.gate_helpers import FakeFileAccess
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


class FakeConnections:
    def __init__(self, tables_by_con: dict[str, list[TableRef]] | None = None) -> None:
        self.tables_by_con = tables_by_con or {}

    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]:
        return self.tables_by_con.get(connection_id, [])


def _make_ctx(project_id: str, agent: str = "diagnoser") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_test_1",
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def fake_results() -> FakeResults:
    return FakeResults()


@pytest.fixture
def fake_severity() -> FakeSeverityPolicy:
    return FakeSeverityPolicy()


@pytest.fixture
def fake_maintenance() -> FakeMaintenance:
    return FakeMaintenance()


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def incidents_service(
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
async def test_tools_risk_class_and_registry_presence(
    databases: Databases,
    incidents_service: IncidentsService,
    fake_clock: FakeClock,
) -> None:
    data_summaries = AsyncMock(spec=DataSummaries)
    connections = FakeConnections()
    handoffs = FakeHandoffs()

    registry = default_registry(
        databases=databases,
        incidents=incidents_service,
        handoffs=handoffs,  # type: ignore[arg-type]
        data_summaries=data_summaries,
        connections=connections,  # type: ignore[arg-type]
        clock=fake_clock,
    )

    for name in [INCIDENT_UPDATE, INCIDENT_MERGE, INCIDENT_SPLIT, INCIDENT_FOLLOW_UP]:
        tool = registry.get(name)
        assert tool is not None, f"Tool {name} should be registered in default_registry"
        assert tool.risk == RiskClass.READ, f"Tool {name} risk must be READ"


@pytest.mark.asyncio
async def test_incident_update_tool(
    databases: Databases,
    incidents_service: IncidentsService,
    fake_results: FakeResults,
    fake_clock: FakeClock,
) -> None:
    project_id = new_id("prj")
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="users",
        model="churn",
        ran_at=fake_clock.now(),
        detail={"metric": "null_rate", "value": 0.8},
    )
    outcome = await incidents_service.raise_signal(project_id, draft, seen_at=fake_clock.now())
    inc_id = outcome.incident_id

    update_tool = IncidentUpdate(incidents_service)
    ctx = _make_ctx(project_id, agent="data-quality")

    # 1. Update cause and note
    res = await update_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "cause": "Upstream ETL schema dropped column",
            "note": "Investigated by data-quality agent",
        },
    )
    assert not res.is_error
    assert inc_id in res.content

    view = await incidents_service.get(project_id, inc_id)
    assert view is not None
    assert view.cause == "Upstream ETL schema dropped column"

    # 2. Status update to resolved refused while results are still firing
    fake_results.latest_results = [
        StoredResult(
            id="res_1",
            source_id="src_1",
            source_key="key_1",
            seen_at=fake_clock.now(),
            check_id="chk_users",
            category="data_quality",
            origin="agent",
            subject="users",
            model="churn",
            fired=True,
            ran_at=fake_clock.now(),
            detail={},
            source_ref="ref_1",
        )
    ]
    res_fail = await update_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "status": "resolved",
        },
    )
    assert res_fail.is_error
    assert "still firing" in res_fail.content

    # 3. Status update to mitigated succeeds
    res_mit = await update_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "status": "mitigated",
        },
    )
    assert not res_mit.is_error
    view = await incidents_service.get(project_id, inc_id)
    assert view is not None
    assert view.status == "mitigated"


@pytest.mark.asyncio
async def test_incident_merge_tool(
    databases: Databases,
    incidents_service: IncidentsService,
    fake_clock: FakeClock,
) -> None:
    project_id = new_id("prj")
    t0 = fake_clock.now()
    o1 = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            model="churn",
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    o2 = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="orders",
            model="churn",
            ran_at=t0 + timedelta(minutes=5),
            detail={},
        ),
        seen_at=t0,
    )
    inc1 = o1.incident_id
    inc2 = o2.incident_id
    assert inc1 != inc2

    merge_tool = IncidentMerge(incidents_service)
    ctx = _make_ctx(project_id)

    # Error when less than 2 ids
    res_err = await merge_tool.invoke(ctx, {"ids": [inc1]})
    assert res_err.is_error

    # Merge inc1 and inc2
    res = await merge_tool.invoke(ctx, {"ids": [inc1, inc2]})
    assert not res.is_error
    assert f"Merged into incident {inc1}" in res.content

    view1 = await incidents_service.get(project_id, inc1)
    view2 = await incidents_service.get(project_id, inc2)
    assert view1 is not None and view1.status == "open"
    assert view2 is not None and view2.status == "merged"
    assert view2.merged_into == inc1


@pytest.mark.asyncio
async def test_incident_split_tool(
    databases: Databases,
    incidents_service: IncidentsService,
    fake_clock: FakeClock,
) -> None:
    project_id = new_id("prj")
    t0 = fake_clock.now()
    o1 = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            model="churn",
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    o2 = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            model="churn",
            ran_at=t0 + timedelta(minutes=1),
            detail={},
        ),
        seen_at=t0,
    )
    inc_id = o1.incident_id
    sig1 = o1.signal_id
    sig2 = o2.signal_id

    split_tool = IncidentSplit(incidents_service)
    ctx = _make_ctx(project_id)

    # Error when neither id nor incident_id is given
    res_err = await split_tool.invoke(ctx, {"signal_ids": [sig2]})
    assert res_err.is_error

    # Error when signal does not belong to incident
    res_inv = await split_tool.invoke(ctx, {"incident_id": inc_id, "signal_ids": ["sig_unknown"]})
    assert res_inv.is_error

    # Valid split using incident_id
    res = await split_tool.invoke(ctx, {"incident_id": inc_id, "signal_ids": [sig2]})
    assert not res.is_error
    assert "Split created incident inc_" in res.content

    orig_sigs = await incidents_service.signals(project_id, inc_id)
    assert [s.id for s in orig_sigs] == [sig1]


@pytest.mark.asyncio
async def test_incident_follow_up_tool(
    databases: Databases,
    incidents_service: IncidentsService,
    fake_clock: FakeClock,
) -> None:
    project_id = new_id("prj")
    t0 = fake_clock.now()
    o = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="model_performance",
            subject="auc",
            model="cancel",
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    inc_id = o.incident_id

    payload = HandoffPayload.model_validate(sample_handoff_dict())
    handoffs = FakeHandoffs(payload)
    connections = FakeConnections(
        {"hotel": [TableRef(name="labels", files=["labels.parquet"], format="parquet")]}
    )

    data_summaries = AsyncMock(spec=DataSummaries)
    data_summaries.profile.return_value = TableProfile(
        row_count=1500,
        columns=[],
        more_columns=0,
        sample_columns=[],
        sample=[],
        timed_out=False,
        incomplete=False,
        timeout_s=30.0,
    )

    follow_up_tool = IncidentFollowUp(
        incidents=incidents_service,
        databases=databases,
        handoffs=handoffs,  # type: ignore[arg-type]
        data_summaries=data_summaries,
        connections=connections,  # type: ignore[arg-type]
        clock=fake_clock,
    )
    ctx = _make_ctx(project_id, agent="model-performance")

    # 1. Validation: both after_days and after_labels
    res1 = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "after_days": 7,
            "after_labels": 500,
            "note": "Re-check performance",
        },
    )
    assert res1.is_error
    assert "Exactly one of 'after_days' or 'after_labels'" in res1.content

    # 2. Validation: neither
    res2 = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "note": "Re-check performance",
        },
    )
    assert res2.is_error
    assert "Exactly one of 'after_days' or 'after_labels'" in res2.content

    # 3. Validation: <= 0
    res3 = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "after_days": 0,
            "note": "Re-check",
        },
    )
    assert res3.is_error
    assert "must be greater than 0" in res3.content

    # 4. Validation: unknown incident
    res4 = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": "inc_unknown",
            "after_days": 3,
            "note": "Re-check",
        },
    )
    assert res4.is_error
    assert "not found" in res4.content

    # 4b. Validation: model with no labels table in handoff
    o_no_model = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="raw_events",
            model=None,
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    res_no_model = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": o_no_model.incident_id,
            "after_labels": 100,
            "note": "Re-check",
        },
    )
    assert res_no_model.is_error
    assert "no associated model" in res_no_model.content

    # 5. Success: day follow-up
    res_days = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "after_days": 7,
            "note": "Re-check performance after 7 days",
        },
    )
    assert not res_days.is_error
    assert "Follow-up scheduled" in res_days.content

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(incident_follow_ups).where(incident_follow_ups.c.after_days == 7)
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["incident"] == inc_id
        assert r["agent"] == "model-performance"
        assert r["after_days"] == 7
        assert r["after_labels"] is None
        assert r["label_base_count"] is None
        assert r["done"] == 0
        expected_due = (t0 + timedelta(days=7)).isoformat()
        assert r["due"] == expected_due

    # 6. Success: label follow-up
    res_labels = await follow_up_tool.invoke(
        ctx,
        {
            "incident_id": inc_id,
            "after_labels": 250,
            "note": "Re-check performance after 250 new labels",
        },
    )
    assert not res_labels.is_error
    assert "Follow-up scheduled" in res_labels.content
    data_summaries.profile.assert_awaited_once()

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(incident_follow_ups).where(incident_follow_ups.c.after_labels == 250)
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["incident"] == inc_id
        assert r["agent"] == "model-performance"
        assert r["after_labels"] == 250
        assert r["label_base_count"] == 1500
        assert r["due"] is None
        assert r["done"] == 0
