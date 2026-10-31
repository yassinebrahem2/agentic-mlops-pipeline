"""Tests for FixesQueryProvider and TrainingRequestsQueryProvider (Task 5, #490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import IncidentStatus, IncidentView
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.training.constants import RETRAINING_AGENT
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.queries import FixesQueryProvider, TrainingRequestsQueryProvider
from mlops_agents.training.requests import SqliteTraining
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock


class FakeHandoffs:
    def __init__(self, current_handoff: HandoffView | None = None) -> None:
        self._current = current_handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current


class FakeWaker:
    def __init__(self) -> None:
        self.wakes: list[dict[str, Any]] = []

    async def wake(self, *args: Any, **kwargs: Any) -> None:
        pass


class FakeIncidents:
    def __init__(self) -> None:
        self._incidents: dict[str, IncidentView] = {}

    def add(self, inc: IncidentView) -> None:
        self._incidents[inc.id] = inc

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        return self._incidents.get(incident_id)


def make_handoff(model_name: str = "churn_model") -> HandoffView:
    payload_dict = handoff()
    payload_dict["models"][0]["name"] = model_name
    payload_dict["models"][0]["profile"]["label_maturity_days"] = 1
    payload_dict["data_roles"].append(
        {
            "connection": "db_conn",
            "table": "churn_train",
            "role": "training",
            "id_column": "user_id",
            "time_column": "created_at",
        }
    )
    payload = HandoffPayload.model_validate(payload_dict)
    return HandoffView(
        proposal_id="prop_h1",
        version=1,
        approved=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


def make_incident(
    incident_id: str = "inc_1",
    model: str | None = "churn_model",
    status: IncidentStatus = "open",
) -> IncidentView:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    return IncidentView(
        id=incident_id,
        category="model_performance",
        subtype="drift",
        severity="medium",
        status=status,
        cause="Feature drift detected in churn dataset",
        cause_key=f"drift:{model}",
        model=model,
        signal_ids=["sig_1"],
        prs=[],
        issues=[],
        held=False,
        merged_into=None,
        started_at=now,
        detected_at=now,
    )


@pytest.fixture
def project_id() -> str:
    return "prj_queries"


async def test_fixes_query_provider_untrusted_fields(databases: Databases, project_id: str) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    inc = make_incident(incident_id="inc_fix_query", model="churn_model")
    incidents.add(inc)
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    provider = FixesQueryProvider(fixes)

    raw_evidence = "Evidence with <b>html</b> tags"
    raw_why = "Why chosen reasoning"
    raw_why_not = {
        "recalibrate": "Recalibrate not suitable",
        "re_threshold": "Re-threshold not suitable",
    }

    fix_view = await fixes.request(project_id, inc.id, "diagnoser", raw_evidence)
    await fixes.choose(
        project_id,
        fix_view.id,
        agent=RETRAINING_AGENT,
        choice="retrain",
        why=raw_why,
        why_not=raw_why_not,
    )

    page = await provider.query(project_id, MonitoringQuery(what="fixes", id=fix_view.id))
    assert page.total == 1
    assert len(page.rows) == 1
    res = page.rows[0]

    expected_evidence = untrusted(raw_evidence, source="fix_request")
    expected_why = untrusted(raw_why, source="fix_request")
    expected_why_not_recalibrate = untrusted(raw_why_not["recalibrate"], source="fix_request")
    expected_why_not_re_threshold = untrusted(raw_why_not["re_threshold"], source="fix_request")

    assert res.evidence == expected_evidence
    assert res.why == expected_why
    assert res.why_not["recalibrate"] == expected_why_not_recalibrate
    assert res.why_not["re_threshold"] == expected_why_not_re_threshold


async def test_fixes_query_provider_paging_and_filtering(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()

    inc1 = make_incident(incident_id="inc_f1", model="model_a")
    inc2 = make_incident(incident_id="inc_f2", model="model_b")
    inc3 = make_incident(incident_id="inc_f3", model="model_a")
    incidents.add(inc1)
    incidents.add(inc2)
    incidents.add(inc3)

    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    provider = FixesQueryProvider(fixes)

    clock.current = now
    f1 = await fixes.request(project_id, inc1.id, "diagnoser", "Evidence 1")
    clock.current = now + timedelta(minutes=1)
    f2 = await fixes.request(project_id, inc2.id, "diagnoser", "Evidence 2")
    clock.current = now + timedelta(minutes=2)
    f3 = await fixes.request(project_id, inc3.id, "diagnoser", "Evidence 3")

    # Filter by model
    page_model_a = await provider.query(project_id, MonitoringQuery(what="fixes", model="model_a"))
    assert page_model_a.total == 2
    assert {r.id for r in page_model_a.rows} == {f1.id, f3.id}

    # Filter by incident_id
    page_inc2 = await provider.query(project_id, MonitoringQuery(what="fixes", incident_id=inc2.id))
    assert page_inc2.total == 1
    assert page_inc2.rows[0].id == f2.id

    # Filter by subject (aliases to incident_id)
    page_subj = await provider.query(project_id, MonitoringQuery(what="fixes", subject=inc1.id))
    assert page_subj.total == 1
    assert page_subj.rows[0].id == f1.id

    # Paging limit & offset
    page_paged = await provider.query(
        project_id, MonitoringQuery(what="fixes", model="model_a", limit=1, offset=1)
    )
    assert page_paged.total == 2
    assert len(page_paged.rows) == 1
    assert page_paged.truncated is False
    assert page_paged.rows[0].id == f1.id


async def test_training_requests_query_provider_paging_and_filtering(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)
    provider = TrainingRequestsQueryProvider(training)

    clock.current = now
    r1 = await training.create_request(
        project_id=project_id,
        request_id="trq_q1",
        model="model_x",
        hook_id="h1",
        mode="train",
        status="started",
    )

    clock.current = now + timedelta(minutes=1)
    r2 = await training.create_request(
        project_id=project_id,
        request_id="trq_q2",
        model="model_y",
        hook_id="h2",
        mode="train",
        status="linked",
    )

    clock.current = now + timedelta(minutes=2)
    r3 = await training.create_request(
        project_id=project_id,
        request_id="trq_q3",
        model="model_x",
        hook_id="h1",
        mode="train",
        status="failed",
    )

    # Filter by model
    page_model_x = await provider.query(
        project_id, MonitoringQuery(what="training_requests", model="model_x")
    )
    assert page_model_x.total == 2
    assert {r.id for r in page_model_x.rows} == {r1.id, r3.id}

    # Filter by status
    page_linked = await provider.query(
        project_id, MonitoringQuery(what="training_requests", status="linked")
    )
    assert page_linked.total == 1
    assert page_linked.rows[0].id == r2.id

    # Filter by id
    page_by_id = await provider.query(
        project_id, MonitoringQuery(what="training_requests", id="trq_q3")
    )
    assert page_by_id.total == 1
    assert page_by_id.rows[0].id == r3.id

    # Paging limit and offset
    page_page = await provider.query(
        project_id,
        MonitoringQuery(what="training_requests", model="model_x", limit=1, offset=0),
    )
    assert page_page.total == 2
    assert len(page_page.rows) == 1
    assert page_page.truncated is True
    assert page_page.rows[0].id == r3.id  # Newest first

    page_page_2 = await provider.query(
        project_id,
        MonitoringQuery(what="training_requests", model="model_x", limit=1, offset=1),
    )
    assert page_page_2.total == 2
    assert len(page_page_2.rows) == 1
    assert page_page_2.truncated is False
    assert page_page_2.rows[0].id == r1.id
