"""Tests for the Inbox set (`inbox=true` on the incidents list)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import IncidentPatch, SignalDraft
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import verdicts
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
)


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


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


def _make_signal(subject: str, at: datetime) -> SignalDraft:
    return SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject=subject,
        model=None,
        ran_at=at,
        detail={},
    )


@pytest.mark.asyncio
async def test_inbox_returns_active_statuses_and_resolved_without_verdict(
    service: IncidentsService,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Each active status (open, acknowledged, mitigated, reopened) and resolved without verdict

    are returned when inbox=true.
    """
    t0 = fake_clock.now()

    # 1. Open
    out_open = await service.raise_signal(project_id, _make_signal("sub_open", t0), seen_at=t0)
    inc_open_id = out_open.incident_id

    # 2. Acknowledged
    out_ack = await service.raise_signal(
        project_id,
        _make_signal("sub_ack", t0 + timedelta(seconds=1)),
        seen_at=t0 + timedelta(seconds=1),
    )
    inc_ack_id = out_ack.incident_id
    await service.acknowledge(project_id, inc_ack_id, by="user")

    # 3. Mitigated
    out_mit = await service.raise_signal(
        project_id,
        _make_signal("sub_mit", t0 + timedelta(seconds=2)),
        seen_at=t0 + timedelta(seconds=2),
    )
    inc_mit_id = out_mit.incident_id
    await service.update(project_id, inc_mit_id, "user", IncidentPatch(status="mitigated"))

    # 4. Reopened
    out_reopen = await service.raise_signal(
        project_id,
        _make_signal("sub_reopen", t0 + timedelta(seconds=3)),
        seen_at=t0 + timedelta(seconds=3),
    )
    inc_reopen_id = out_reopen.incident_id
    fake_clock.advance(10)
    await service.update(project_id, inc_reopen_id, "user", IncidentPatch(status="resolved"))
    # Raise another signal with same cause_key within reopen window
    fake_clock.advance(10)
    t_reopen = fake_clock.now()
    await service.raise_signal(project_id, _make_signal("sub_reopen", t_reopen), seen_at=t_reopen)
    reopened_inc = await service.get(project_id, inc_reopen_id)
    assert reopened_inc is not None
    assert reopened_inc.status == "reopened"

    # 5. Resolved without verdict
    out_res_no_verdict = await service.raise_signal(
        project_id,
        _make_signal("sub_res_no_verdict", t_reopen + timedelta(seconds=1)),
        seen_at=t_reopen + timedelta(seconds=1),
    )
    inc_res_no_verdict_id = out_res_no_verdict.incident_id
    await service.update(
        project_id, inc_res_no_verdict_id, "user", IncidentPatch(status="resolved")
    )

    page = await service.list(project_id, inbox=True)
    returned_ids = {r.id for r in page.rows}
    assert inc_open_id in returned_ids
    assert inc_ack_id in returned_ids
    assert inc_mit_id in returned_ids
    assert inc_reopen_id in returned_ids
    assert inc_res_no_verdict_id in returned_ids
    assert page.total == 5


@pytest.mark.asyncio
async def test_inbox_excludes_resolved_with_verdict_closed_and_merged(
    service: IncidentsService,
    databases: Databases,
    project_id: str,
) -> None:
    """Resolved incident with a verdict, closed incident, and merged incident are NOT returned when inbox=true."""
    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Resolved incident WITH a verdict
    out_res_verdict = await service.raise_signal(
        project_id, _make_signal("sub_res_verdict", t0), seen_at=t0
    )
    inc_res_verdict_id = out_res_verdict.incident_id
    await service.update(project_id, inc_res_verdict_id, "user", IncidentPatch(status="resolved"))

    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            verdicts.insert().values(
                incident_id=inc_res_verdict_id,
                value="real_issue",
                source="user",
                check_versions="{}",
                created=t0.isoformat(),
            )
        )

    # 2. Closed incident
    out_closed = await service.raise_signal(
        project_id,
        _make_signal("sub_closed", t0 + timedelta(seconds=1)),
        seen_at=t0 + timedelta(seconds=1),
    )
    inc_closed_id = out_closed.incident_id
    await service.close(project_id, inc_closed_id, by="user")

    # 3. Merged incident
    out_primary = await service.raise_signal(
        project_id,
        _make_signal("sub_primary", t0 + timedelta(seconds=2)),
        seen_at=t0 + timedelta(seconds=2),
    )
    out_secondary = await service.raise_signal(
        project_id,
        _make_signal("sub_secondary", t0 + timedelta(seconds=3)),
        seen_at=t0 + timedelta(seconds=3),
    )
    # Merging secondary into primary: secondary will have status="merged"
    await service.merge(
        project_id, [out_primary.incident_id, out_secondary.incident_id], agent="user"
    )
    secondary_inc = await service.get(project_id, out_secondary.incident_id)
    assert secondary_inc is not None
    assert secondary_inc.status == "merged"

    # List with inbox=true
    page = await service.list(project_id, inbox=True)
    returned_ids = {r.id for r in page.rows}

    assert inc_res_verdict_id not in returned_ids
    assert inc_closed_id not in returned_ids
    assert out_secondary.incident_id not in returned_ids
    # The primary incident (still open) is in the inbox
    assert out_primary.incident_id in returned_ids


@pytest.mark.asyncio
async def test_inbox_preserves_severity_ranking(
    service: IncidentsService,
    project_id: str,
) -> None:
    """Severity ranking is preserved (critical > high > medium > low, then newest detected_at)."""
    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # Low: t0 + 10s
    out_low = await service.raise_signal(
        project_id,
        _make_signal("sub_low", t0 + timedelta(seconds=10)),
        seen_at=t0 + timedelta(seconds=10),
    )
    await service.set_severity(project_id, out_low.incident_id, "low", reason="test", by="user")

    # High: t0 + 20s
    out_high = await service.raise_signal(
        project_id,
        _make_signal("sub_high", t0 + timedelta(seconds=20)),
        seen_at=t0 + timedelta(seconds=20),
    )
    await service.set_severity(project_id, out_high.incident_id, "high", reason="test", by="user")

    # Critical older: t0 + 30s
    out_crit_old = await service.raise_signal(
        project_id,
        _make_signal("sub_crit_old", t0 + timedelta(seconds=30)),
        seen_at=t0 + timedelta(seconds=30),
    )
    await service.set_severity(
        project_id, out_crit_old.incident_id, "critical", reason="test", by="user"
    )

    # Critical newer: t0 + 40s
    out_crit_new = await service.raise_signal(
        project_id,
        _make_signal("sub_crit_new", t0 + timedelta(seconds=40)),
        seen_at=t0 + timedelta(seconds=40),
    )
    await service.set_severity(
        project_id, out_crit_new.incident_id, "critical", reason="test", by="user"
    )

    # Medium: t0 + 50s
    out_med = await service.raise_signal(
        project_id,
        _make_signal("sub_med", t0 + timedelta(seconds=50)),
        seen_at=t0 + timedelta(seconds=50),
    )
    await service.set_severity(project_id, out_med.incident_id, "medium", reason="test", by="user")

    page = await service.list(project_id, inbox=True)
    assert [r.id for r in page.rows] == [
        out_crit_new.incident_id,
        out_crit_old.incident_id,
        out_high.incident_id,
        out_med.incident_id,
        out_low.incident_id,
    ]


@pytest.mark.asyncio
async def test_inbox_pagination(
    service: IncidentsService,
    databases: Databases,
    project_id: str,
) -> None:
    """Paging with inbox=true returns the right total, offset, and sliced rows.

    Mixes in excluded rows (a closed one, and a resolved one with a verdict) with newer
    timestamps so that an unfiltered count query or post-fetch filtering fails the test.
    """
    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    created_ids: list[str] = []
    for i in range(5):
        out = await service.raise_signal(
            project_id,
            _make_signal(f"sub_page_{i}", t0 + timedelta(seconds=i)),
            seen_at=t0 + timedelta(seconds=i),
        )
        created_ids.append(out.incident_id)

    # Mixed in excluded row 1: closed incident with newer timestamp
    out_closed = await service.raise_signal(
        project_id,
        _make_signal("sub_excluded_closed", t0 + timedelta(seconds=10)),
        seen_at=t0 + timedelta(seconds=10),
    )
    await service.close(project_id, out_closed.incident_id, by="user")

    # Mixed in excluded row 2: resolved incident with a verdict and newer timestamp
    out_verdict = await service.raise_signal(
        project_id,
        _make_signal("sub_excluded_verdict", t0 + timedelta(seconds=11)),
        seen_at=t0 + timedelta(seconds=11),
    )
    await service.update(
        project_id, out_verdict.incident_id, "user", IncidentPatch(status="resolved")
    )
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            verdicts.insert().values(
                incident_id=out_verdict.incident_id,
                value="real_issue",
                source="user",
                check_versions="{}",
                created=t0.isoformat(),
            )
        )

    # Unfiltered total is 7 (5 inbox + 2 excluded)
    unfiltered = await service.list(project_id)
    assert unfiltered.total == 7

    # Page 1: limit 2, offset 0 -> inbox total is 5, rows contain newest 2 inbox items
    p1 = await service.list(project_id, inbox=True, limit=2, offset=0)
    assert p1.total == 5
    assert len(p1.rows) == 2
    assert p1.truncated is True

    # Page 2: limit 2, offset 2 -> inbox total is 5
    p2 = await service.list(project_id, inbox=True, limit=2, offset=2)
    assert p2.total == 5
    assert len(p2.rows) == 2
    assert p2.truncated is True

    # Page 3: limit 2, offset 4 -> inbox total is 5
    p3 = await service.list(project_id, inbox=True, limit=2, offset=4)
    assert p3.total == 5
    assert len(p3.rows) == 1
    assert p3.truncated is False

    # Check distinct items across pages: exactly the 5 inbox items in reverse chronological order
    all_paged_ids = [r.id for r in p1.rows] + [r.id for r in p2.rows] + [r.id for r in p3.rows]
    assert all_paged_ids == list(reversed(created_ids))
    assert out_closed.incident_id not in all_paged_ids
    assert out_verdict.incident_id not in all_paged_ids


@pytest.mark.asyncio
async def test_inbox_status_conflict_raises_422(
    service: IncidentsService,
    project_id: str,
) -> None:
    """Providing both status and inbox=true raises DomainError(inbox_status_conflict, 422)."""
    with pytest.raises(DomainError) as exc_info:
        await service.list(project_id, status=["open"], inbox=True)

    assert exc_info.value.code == "inbox_status_conflict"
    assert exc_info.value.status == 422
    assert exc_info.value.message == "Cannot combine 'status' and 'inbox' filters."


@pytest.mark.asyncio
async def test_inbox_false_or_omitted_behaves_as_before(
    service: IncidentsService,
    project_id: str,
) -> None:
    """Calling without inbox (default False) preserves existing status and unfiltered behavior."""
    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    out_open = await service.raise_signal(project_id, _make_signal("sub_1", t0), seen_at=t0)
    out_closed = await service.raise_signal(
        project_id,
        _make_signal("sub_2", t0 + timedelta(seconds=1)),
        seen_at=t0 + timedelta(seconds=1),
    )
    await service.close(project_id, out_closed.incident_id, by="user")

    # Without inbox: returns both open and closed
    page_default = await service.list(project_id)
    assert page_default.total == 2
    default_ids = {r.id for r in page_default.rows}
    assert out_open.incident_id in default_ids
    assert out_closed.incident_id in default_ids

    page_false = await service.list(project_id, inbox=False)
    assert page_false.total == 2

    # Status filter works when inbox=False
    page_closed = await service.list(project_id, status=["closed"], inbox=False)
    assert page_closed.total == 1
    assert page_closed.rows[0].id == out_closed.incident_id


@pytest.mark.asyncio
async def test_inbox_http_api_routes(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    """End-to-end HTTP tests for ?inbox=true on the incidents list route."""
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    inc_service = services.incidents
    databases = services.databases

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. Open
    out_open = await inc_service.raise_signal(project_id, _make_signal("http_open", t0), seen_at=t0)

    # 2. Resolved without verdict
    out_res = await inc_service.raise_signal(
        project_id,
        _make_signal("http_res", t0 + timedelta(seconds=1)),
        seen_at=t0 + timedelta(seconds=1),
    )
    await inc_service.update(
        project_id, out_res.incident_id, "user", IncidentPatch(status="resolved")
    )

    # 3. Resolved with verdict
    out_verdict = await inc_service.raise_signal(
        project_id,
        _make_signal("http_verdict", t0 + timedelta(seconds=2)),
        seen_at=t0 + timedelta(seconds=2),
    )
    await inc_service.update(
        project_id, out_verdict.incident_id, "user", IncidentPatch(status="resolved")
    )
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            verdicts.insert().values(
                incident_id=out_verdict.incident_id,
                value="real_issue",
                source="user",
                check_versions="{}",
                created=t0.isoformat(),
            )
        )

    # 4. Closed
    out_closed = await inc_service.raise_signal(
        project_id,
        _make_signal("http_closed", t0 + timedelta(seconds=3)),
        seen_at=t0 + timedelta(seconds=3),
    )
    await inc_service.close(project_id, out_closed.incident_id, by="user")

    # Query with inbox=true
    res = await client.get(f"/api/projects/{project_id}/incidents?inbox=true")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    returned_ids = {r["id"] for r in data["rows"]}
    assert returned_ids == {out_open.incident_id, out_res.incident_id}

    # Query with both status and inbox=true -> 422
    res_conflict = await client.get(f"/api/projects/{project_id}/incidents?inbox=true&status=open")
    assert res_conflict.status_code == 422
    err_data = res_conflict.json()
    assert err_data["code"] == "inbox_status_conflict"

    # Query without inbox: returns all 4
    res_all = await client.get(f"/api/projects/{project_id}/incidents")
    assert res_all.status_code == 200
    assert res_all.json()["total"] == 4
