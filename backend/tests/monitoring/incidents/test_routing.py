"""Tests for OnCallRouter (Task 4 of Plan #464)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from mlops_agents.agents.tools.issues import set_issue_min_priority
from mlops_agents.monitoring.incidents.contract import IncidentView, cause_key
from mlops_agents.monitoring.incidents.routing import OnCallRouter
from mlops_agents.notifications.contract import OnCallPerson
from mlops_agents.proposals.payloads import Contact, OnCallHours
from mlops_agents.storage.databases import Databases


def _make_incident(severity: str) -> IncidentView:
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    return IncidentView(
        id="inc_1",
        category="operational",  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        status="open",
        cause_key=cause_key("scoring_job", None),
        started_at=now,
        detected_at=now,
    )


def _make_person(
    id: str,
    name: str,
    order: int,
    start: str = "09:00",
    end: str = "17:00",
    tz: str = "Europe/Paris",
) -> OnCallPerson:
    return OnCallPerson(
        id=id,
        name=name,
        contacts=[Contact(channel="discord", address="123456789")],
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"], start=start, end=end
        ),
        timezone=tz,
        escalation_order=order,
    )


@pytest.mark.anyio
async def test_router_critical_in_hours(databases: Databases) -> None:
    p1 = _make_person("p1", "Alice", order=1, start="09:00", end="17:00", tz="Europe/Paris")
    p2 = _make_person("p2", "Bob", order=0, start="09:00", end="17:00", tz="Europe/Paris")

    fake_on_call = AsyncMock()
    # Notice p2 has lower escalation_order (0) than p1 (1)
    fake_on_call.list.return_value = [p1, p2]

    router = OnCallRouter(on_call=fake_on_call, databases=databases)

    # 2026-10-31 is Saturday. 12:00 UTC = 13:00 Europe/Paris (in hours 09:00 - 17:00)
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    inc = _make_incident(severity="critical")

    plan = await router.plan("prj_routing", inc, now)

    assert plan.inbox is True
    assert len(plan.deliveries) == 1
    assert plan.deliveries[0].person == "p2"  # lower escalation_order
    assert plan.deliveries[0].when == "now"
    assert plan.deliveries[0].escalation_step == 0


@pytest.mark.anyio
async def test_router_critical_at_night_any_hour(databases: Databases) -> None:
    # Paris time: 02:00 UTC = 03:00 Europe/Paris (night, out of hours 09:00 - 17:00)
    p1 = _make_person("p1", "Alice", order=0, start="09:00", end="17:00", tz="Europe/Paris")
    p2 = _make_person("p2", "Bob", order=1, start="09:00", end="17:00", tz="Europe/Paris")

    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [p2, p1]

    router = OnCallRouter(on_call=fake_on_call, databases=databases)

    now = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
    inc = _make_incident(severity="critical")

    plan = await router.plan("prj_routing", inc, now)

    assert plan.inbox is True
    assert len(plan.deliveries) == 1
    # Critical reaches first person by escalation_order at any hour
    assert plan.deliveries[0].person == "p1"
    assert plan.deliveries[0].when == "now"


@pytest.mark.anyio
async def test_router_high_in_hours_and_out_of_hours(databases: Databases) -> None:
    p1 = _make_person("p1", "Alice", order=0, start="09:00", end="17:00", tz="America/New_York")
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [p1]

    router = OnCallRouter(on_call=fake_on_call, databases=databases)

    # 1. In hours: 14:00 UTC = 10:00 EDT (in hours)
    now_in = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
    inc = _make_incident(severity="high")
    plan_in = await router.plan("prj_routing", inc, now_in)

    assert plan_in.inbox is True
    assert len(plan_in.deliveries) == 1
    assert plan_in.deliveries[0].person == "p1"
    assert plan_in.deliveries[0].when == "now"

    # 2. Out of hours: 03:00 UTC = 23:00 EDT previous night (out of hours)
    now_out = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)
    plan_out = await router.plan("prj_routing", inc, now_out)

    assert plan_out.inbox is True
    assert len(plan_out.deliveries) == 1
    assert plan_out.deliveries[0].person == "p1"
    assert plan_out.deliveries[0].when == "morning_report"


@pytest.mark.anyio
async def test_router_medium_and_low_inbox_only(databases: Databases) -> None:
    p1 = _make_person("p1", "Alice", order=0)
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [p1]

    router = OnCallRouter(on_call=fake_on_call, databases=databases)

    now = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)

    plan_med = await router.plan("prj_routing", _make_incident("medium"), now)
    assert plan_med.deliveries == []
    assert plan_med.inbox is True

    plan_low = await router.plan("prj_routing", _make_incident("low"), now)
    assert plan_low.deliveries == []
    assert plan_low.inbox is True


@pytest.mark.anyio
async def test_router_open_issue_threshold(databases: Databases) -> None:
    project_id = "prj_thresh"
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = []

    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    # Default min priority is "medium" (unless changed)
    # Low -> False
    plan_low = await router.plan(project_id, _make_incident("low"), now)
    assert plan_low.open_issue is False

    # Medium -> True
    plan_med = await router.plan(project_id, _make_incident("medium"), now)
    assert plan_med.open_issue is True

    # High -> True
    plan_high = await router.plan(project_id, _make_incident("high"), now)
    assert plan_high.open_issue is True

    # Critical -> True
    plan_crit = await router.plan(project_id, _make_incident("critical"), now)
    assert plan_crit.open_issue is True

    # Set threshold to "critical"
    await set_issue_min_priority(databases, project_id, "critical")

    plan_high2 = await router.plan(project_id, _make_incident("high"), now)
    assert plan_high2.open_issue is False

    plan_crit2 = await router.plan(project_id, _make_incident("critical"), now)
    assert plan_crit2.open_issue is True
