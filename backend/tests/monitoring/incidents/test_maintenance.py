"""Tests for MaintenanceWindows, MaintenanceKind, WindowsQueryProvider, and window/snooze routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.clock import to_iso
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    MaintenancePayload,
    SignalDraft,
    WindowSpec,
)
from mlops_agents.monitoring.incidents.maintenance import (
    MaintenanceKind,
    MaintenanceWindows,
    WindowsQueryProvider,
)
from mlops_agents.monitoring.incidents.reactor import Reactor
from mlops_agents.monitoring.incidents.routing import OnCallRouter
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.results.contract import ResultQuery, ResultRecord
from mlops_agents.notifications.contract import OnCallPerson
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Contact, OnCallHours
from mlops_agents.storage.databases import Databases
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.monitoring.incidents.test_reactor import DummyGitHubAuth, TaskDouble
from tests.providers.fake_clock import FakeClock


def make_on_call_person() -> OnCallPerson:
    return OnCallPerson(
        id="alice",
        name="Alice",
        contacts=[Contact(channel="discord", address="user_123")],  # type: ignore[arg-type]
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=0,
    )


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


@pytest.mark.asyncio
async def test_window_suspends_only_inside_time_range(tmp_path: Path) -> None:
    """A window suspends a feed's key and a connection only inside its time range."""
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    clock = FakeClock(start=datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC))
    windows = MaintenanceWindows(databases, clock)
    project_id = "prj_maint1"

    t1 = datetime(2026, 11, 1, 2, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
    t3 = datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC)
    t_before = t1 - timedelta(seconds=1)
    t_after = t3 + timedelta(seconds=1)

    spec = WindowSpec(
        start=t1,
        end=t3,
        scope=["feed:orders", "con_warehouse"],
        reason="Scheduled DB maintenance",
    )
    win = await windows.create(project_id, spec, created_by="user")
    assert win.id.startswith("win_")
    assert win.created_by == "user"
    assert win.proposal_id is None

    feed_key = ExpectationKey(kind="feed", ref="feed:orders")
    conn_scope = "con_warehouse"

    # Before window: neither is suspended
    assert not await windows.suspended(project_id, feed_key, t_before)
    assert not await windows.suspended(project_id, conn_scope, t_before)

    # At exact start: both are suspended (start <= at < end)
    assert await windows.suspended(project_id, feed_key, t1)
    assert await windows.suspended(project_id, conn_scope, t1)

    # In middle of window: both are suspended
    assert await windows.suspended(project_id, feed_key, t2)
    assert await windows.suspended(project_id, conn_scope, t2)

    # Unrelated scope during window: not suspended
    assert not await windows.suspended(project_id, "con_other", t2)
    assert not await windows.suspended(
        project_id, ExpectationKey(kind="feed", ref="feed:customers"), t2
    )

    # At exact end: not suspended (at < end required)
    assert not await windows.suspended(project_id, feed_key, t3)
    assert not await windows.suspended(project_id, conn_scope, t3)

    # After window: not suspended
    assert not await windows.suspended(project_id, feed_key, t_after)
    assert not await windows.suspended(project_id, conn_scope, t_after)


@pytest.mark.asyncio
async def test_record_in_scope_raises_no_signal_and_is_stored(
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """A record in scope raises no signal and is stored."""
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services

    t1 = datetime(2026, 11, 1, 2, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
    t3 = datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC)

    # Create active window for warehouse connection
    await services.maintenance_windows.create(
        project_id,
        WindowSpec(
            start=t1,
            end=t3,
            scope=["con_warehouse"],
            reason="Warehouse migration",
        ),
        created_by="user",
    )

    # Ingest record in scope
    rec_in = ResultRecord(
        check_id="chk_1",
        origin="agent",
        ran_at=t2,
        fired=True,
        category="data_quality",
        subject="orders",
        detail={"score": 0.1},
        source_ref="results/r1.json",
    )
    stored = await services.results.ingest(
        project_id,
        "file:con_warehouse:results/*.json",
        [("r1", rec_in)],
        seen_at=t2,
    )
    assert len(stored) == 1

    # Deliver to observers
    await services.results.deliver_undelivered(project_id, t2)

    # Verify the record IS stored in results
    res_page = await services.results.query(project_id, ResultQuery(subject="orders"))
    assert res_page.total == 1
    assert res_page.rows[0].fired is True

    # But NO incident was opened
    inc_page = await services.incidents.list(project_id)
    assert inc_page.total == 0

    # Record out of scope
    rec_out = ResultRecord(
        check_id="chk_2",
        origin="agent",
        ran_at=t2,
        fired=True,
        category="data_quality",
        subject="users",
        detail={"score": 0.1},
        source_ref="results/r2.json",
    )
    stored_out = await services.results.ingest(
        project_id,
        "file:con_other:results/*.json",
        [("r2", rec_out)],
        seen_at=t2,
    )
    assert len(stored_out) == 1

    # Deliver to observers
    await services.results.deliver_undelivered(project_id, t2)

    # Now an incident IS opened for the out of scope record
    inc_page_after = await services.incidents.list(project_id)
    assert inc_page_after.total == 1


@pytest.mark.asyncio
async def test_apply_twice_stores_one_window_idempotently(tmp_path: Path) -> None:
    """Apply twice stores one window (idempotent, win_ ID)."""
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    clock = FakeClock(start=datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC))
    windows = MaintenanceWindows(databases, clock)
    kind = MaintenanceKind(windows=windows)
    project_id = "prj_maint3"

    t1 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    payload = MaintenancePayload(
        window=WindowSpec(
            start=t1,
            end=t2,
            scope=["feed:metrics", "con_postgres"],
            reason="Postgres failover drill",
        )
    )

    proposal_id = "prp_maint_test1"

    # Preview and key tests
    preview = kind.preview(payload)
    assert preview.kind == "text"
    assert "feed:metrics, con_postgres" in preview.body
    assert "Postgres failover drill" in preview.body
    assert kind.key(payload) == f"Postgres failover drill:{t1.isoformat()}"
    assert kind.warnings(payload) == []
    assert kind.credentials_needed(payload) == []

    # First apply
    res1 = await kind.apply(project_id, proposal_id, payload)
    assert len(res1.applied) == 1
    assert res1.failed == {}
    win_part = res1.applied[0]
    assert win_part.startswith("window:win_")
    win_id = win_part.removeprefix("window:")

    # Second apply (idempotent)
    res2 = await kind.apply(project_id, proposal_id, payload)
    assert res2 == res1

    # Exactly one window exists with proposal_id
    stored_windows = await windows.list(project_id)
    assert len(stored_windows) == 1
    assert stored_windows[0].id == win_id
    assert stored_windows[0].proposal_id == proposal_id
    assert stored_windows[0].created_by == "proposal"
    assert stored_windows[0].scope == ["feed:metrics", "con_postgres"]


@pytest.mark.asyncio
async def test_window_query_provider(tmp_path: Path) -> None:
    """WindowsQueryProvider lists, filters by id, and paginates."""
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    clock = FakeClock(start=datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC))
    windows = MaintenanceWindows(databases, clock)
    provider = WindowsQueryProvider(windows=windows)
    project_id = "prj_maintqp"

    t1 = datetime(2026, 11, 1, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 1, 2, 0, 0, tzinfo=UTC)

    w1 = await windows.create(
        project_id,
        WindowSpec(start=t1, end=t2, scope=["feed:1"], reason="r1"),
        created_by="user",
    )
    clock.advance(seconds=10)
    w2 = await windows.create(
        project_id,
        WindowSpec(start=t1, end=t2, scope=["feed:2"], reason="r2"),
        created_by="user",
    )

    page_all = await provider.query(project_id, MonitoringQuery(what="windows"))
    assert page_all.total == 2
    assert [w.id for w in page_all.rows] == [w2.id, w1.id]

    page_filtered = await provider.query(project_id, MonitoringQuery(what="windows", id=w1.id))
    assert page_filtered.total == 1
    assert page_filtered.rows[0].id == w1.id

    page_paged = await provider.query(
        project_id, MonitoringQuery(what="windows", limit=1, offset=1)
    )
    assert page_paged.total == 2
    assert len(page_paged.rows) == 1
    assert page_paged.rows[0].id == w1.id


@pytest.mark.asyncio
async def test_window_routes_create_list_and_delete(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """Window routes create, list, and delete (404 on unknown window)."""
    project_id = await add_local_project(app, tmp_path, git_env)

    # 1. Initially empty
    list_res = await client.get(f"/api/projects/{project_id}/maintenance-windows")
    assert list_res.status_code == 200
    assert list_res.json() == []

    # 2. Create window
    t_start = datetime(2026, 11, 1, 2, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC)
    create_body = {
        "start": to_iso(t_start),
        "end": to_iso(t_end),
        "scope": ["feed:orders", "con_warehouse"],
        "reason": "Database indexing",
    }
    create_res = await client.post(
        f"/api/projects/{project_id}/maintenance-windows",
        json=create_body,
    )
    assert create_res.status_code == 201
    created_win = create_res.json()
    win_id = created_win["id"]
    assert win_id.startswith("win_")
    assert created_win["reason"] == "Database indexing"
    assert created_win["scope"] == ["feed:orders", "con_warehouse"]
    assert created_win["created_by"] == "user"
    assert created_win["proposal_id"] is None

    # 3. List contains the window
    list_res2 = await client.get(f"/api/projects/{project_id}/maintenance-windows")
    assert list_res2.status_code == 200
    wins = list_res2.json()
    assert len(wins) == 1
    assert wins[0]["id"] == win_id

    # 4. Delete window
    del_res = await client.delete(f"/api/projects/{project_id}/maintenance-windows/{win_id}")
    assert del_res.status_code == 204

    # 5. List is now empty
    list_res3 = await client.get(f"/api/projects/{project_id}/maintenance-windows")
    assert list_res3.status_code == 200
    assert list_res3.json() == []

    # 6. Delete again gives 404
    del_res2 = await client.delete(f"/api/projects/{project_id}/maintenance-windows/{win_id}")
    assert del_res2.status_code == 404
    err_json = del_res2.json()
    assert err_json["code"] == "window_not_found"


@pytest.mark.asyncio
async def test_snooze_route_sets_snoozed_until_and_snoozed_incident_pages_no_one(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """Snooze route sets snoozed_until and a snoozed incident pages no one."""
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    inc_service = services.incidents
    clock = services.clock

    now = clock.now()
    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="job_pipeline.py",
        ran_at=now,
        detail={},
    )
    outcome = await inc_service.raise_signal(project_id, draft, now)
    inc_id = outcome.incident_id

    # Set critical severity
    await inc_service.set_severity(
        project_id, inc_id, "critical", reason="critical outage", by="user"
    )

    # Initial incident not snoozed
    inc_before = await inc_service.get(project_id, inc_id)
    assert inc_before is not None
    assert inc_before.snoozed_until is None

    # Snooze incident via route for 2 hours
    snooze_until = now + timedelta(hours=2)
    snooze_res = await client.post(
        f"/api/projects/{project_id}/incidents/{inc_id}/snooze",
        json={"until": to_iso(snooze_until)},
    )
    assert snooze_res.status_code == 200
    snooze_data = snooze_res.json()
    assert snooze_data["snoozed_until"] is not None

    # Verify incident state in service
    inc_after = await inc_service.get(project_id, inc_id)
    assert inc_after is not None
    assert inc_after.snoozed_until is not None

    # Now verify that reactor pages no one while snoozed
    fake_notifier = AsyncMock()
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [make_on_call_person()]
    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=services.databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()
    http = httpx2.AsyncClient()

    reactor = Reactor(
        incidents=inc_service,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=services.events,
        clock=clock,
    )

    # Simulate an incident change while snoozed
    change = IncidentChange(
        incident_id=inc_id,
        kind="severity_raised",
        before="high",
        after="critical",
    )
    await reactor.changed(project_id, change)

    # Snoozed incident pages no one!
    assert fake_notifier.notify.call_count == 0
    assert fake_dispatch.changed.call_count == 0


_T1 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def _bad_specs() -> list[WindowSpec]:
    return [
        WindowSpec(start=_T2, end=_T1, scope=["feed:a"], reason="reversed"),
        WindowSpec(start=_T1, end=_T1, scope=["feed:a"], reason="empty range"),
        WindowSpec(start=_T1, end=_T2, scope=[], reason="no scope"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2])
async def test_create_refuses_an_inert_window(tmp_path: Path, index: int) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    windows = MaintenanceWindows(databases, FakeClock(start=_T1))

    with pytest.raises(DomainError) as refused:
        await windows.create("prj_inert", _bad_specs()[index], created_by="user")

    assert refused.value.status == 422
    assert await windows.list("prj_inert") == []


@pytest.mark.asyncio
async def test_apply_refuses_an_inert_window_without_storing_it(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    windows = MaintenanceWindows(databases, FakeClock(start=_T1))
    kind = MaintenanceKind(windows=windows)

    with pytest.raises(DomainError) as refused:
        await kind.apply("prj_inert", "prp_inert", MaintenancePayload(window=_bad_specs()[2]))

    assert refused.value.status == 422
    assert await windows.list("prj_inert") == []


@pytest.mark.asyncio
async def test_create_accepts_a_naive_start_as_utc(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    windows = MaintenanceWindows(databases, FakeClock(start=_T1))

    ok = WindowSpec(start=_T1.replace(tzinfo=None), end=_T2, scope=["feed:a"], reason="naive")
    win = await windows.create("prj_naive", ok, created_by="user")
    assert win.start == _T1

    reversed_naive = WindowSpec(
        start=_T2.replace(tzinfo=None), end=_T1, scope=["feed:a"], reason="naive reversed"
    )
    with pytest.raises(DomainError):
        await windows.create("prj_naive", reversed_naive, created_by="user")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"start": to_iso(_T2), "end": to_iso(_T1)}, {"scope": []}],
)
async def test_window_route_refuses_an_inert_window(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    overrides: dict[str, object],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    body: dict[str, object] = {
        "start": to_iso(_T1),
        "end": to_iso(_T2),
        "scope": ["feed:orders"],
        "reason": "x",
        **overrides,
    }

    res = await client.post(f"/api/projects/{project_id}/maintenance-windows", json=body)

    assert res.status_code == 422
    assert res.json()["code"] == "window_inert"
    listed = await client.get(f"/api/projects/{project_id}/maintenance-windows")
    assert listed.json() == []


async def _live_incident(app: FastAPI, project_id: str) -> str:
    services: Services = app.state.services
    now = services.clock.now()
    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="job_pipeline.py",
        ran_at=now,
        detail={},
    )
    return (await services.incidents.raise_signal(project_id, draft, now)).incident_id


@pytest.mark.asyncio
async def test_snooze_route_refuses_a_past_time_and_a_closed_incident(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    inc_id = await _live_incident(app, project_id)
    url = f"/api/projects/{project_id}/incidents/{inc_id}/snooze"
    now = services.clock.now()

    past = await client.post(url, json={"until": to_iso(now - timedelta(minutes=1))})
    assert past.status_code == 422
    naive_past = await client.post(
        url, json={"until": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}
    )
    assert naive_past.status_code == 422
    unknown = await client.post(
        f"/api/projects/{project_id}/incidents/inc_nope/snooze",
        json={"until": to_iso(now + timedelta(hours=1))},
    )
    assert unknown.status_code == 404

    naive_future = await client.post(
        url, json={"until": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")}
    )
    assert naive_future.status_code == 200

    await services.incidents.close(project_id, inc_id)
    closed = await client.post(url, json={"until": to_iso(now + timedelta(hours=1))})
    assert closed.status_code == 409
