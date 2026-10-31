"""Tests for incident routes: list ranking, paging, status filter, 404, timeline, acknowledge, close."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.monitoring.incidents.contract import SignalDraft
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


@pytest.mark.asyncio
async def test_incident_routes_end_to_end(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    inc_service = services.incidents
    event_log = services.events

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # Create 4 incidents with different severities and timestamps:
    # 1: Low, t0 + 10s
    d_low = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_low",
        model=None,
        ran_at=t0 + timedelta(seconds=10),
        detail={},
    )
    out_low = await inc_service.raise_signal(project_id, d_low, seen_at=t0 + timedelta(seconds=10))
    await inc_service.set_severity(project_id, out_low.incident_id, "low", reason="test", by="user")

    # 2: High, t0 + 20s
    d_high = SignalDraft(
        kind="fired_result",
        category="drift",
        subject="table_high",
        model=None,
        ran_at=t0 + timedelta(seconds=20),
        detail={},
    )
    out_high = await inc_service.raise_signal(
        project_id, d_high, seen_at=t0 + timedelta(seconds=20)
    )
    await inc_service.set_severity(
        project_id, out_high.incident_id, "high", reason="test", by="user"
    )

    # 3: Critical older, t0 + 30s
    d_crit1 = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="table_crit1",
        model=None,
        ran_at=t0 + timedelta(seconds=30),
        detail={},
    )
    out_crit1 = await inc_service.raise_signal(
        project_id, d_crit1, seen_at=t0 + timedelta(seconds=30)
    )
    await inc_service.set_severity(
        project_id, out_crit1.incident_id, "critical", reason="test", by="user"
    )

    # 4: Critical newer, t0 + 40s
    d_crit2 = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="table_crit2",
        model=None,
        ran_at=t0 + timedelta(seconds=40),
        detail={},
    )
    out_crit2 = await inc_service.raise_signal(
        project_id, d_crit2, seen_at=t0 + timedelta(seconds=40)
    )
    await inc_service.set_severity(
        project_id, out_crit2.incident_id, "critical", reason="test", by="user"
    )

    # 1. Test ranking: critical first (newest detected_at first), then high, then low
    res_list = await client.get(f"/api/projects/{project_id}/incidents")
    assert res_list.status_code == 200
    page_data = res_list.json()
    assert page_data["total"] == 4
    rows = page_data["rows"]
    # rows[0] should be out_crit2, rows[1] should be out_crit1, rows[2] out_high, rows[3] out_low
    assert rows[0]["id"] == out_crit2.incident_id
    assert rows[1]["id"] == out_crit1.incident_id
    assert rows[2]["id"] == out_high.incident_id
    assert rows[3]["id"] == out_low.incident_id

    # 2. Test paging
    res_paged = await client.get(f"/api/projects/{project_id}/incidents?limit=2&offset=1")
    assert res_paged.status_code == 200
    paged_data = res_paged.json()
    assert len(paged_data["rows"]) == 2
    assert paged_data["rows"][0]["id"] == out_crit1.incident_id
    assert paged_data["rows"][1]["id"] == out_high.incident_id

    # 3. Test acknowledge route
    res_ack = await client.post(
        f"/api/projects/{project_id}/incidents/{out_high.incident_id}/acknowledge"
    )
    assert res_ack.status_code == 200
    assert res_ack.json()["status"] == "acknowledged"

    # 4. Test status filter
    res_filtered = await client.get(f"/api/projects/{project_id}/incidents?status=acknowledged")
    assert res_filtered.status_code == 200
    filt_data = res_filtered.json()
    assert filt_data["total"] == 1
    assert filt_data["rows"][0]["id"] == out_high.incident_id

    # 5. Test close route
    res_close = await client.post(
        f"/api/projects/{project_id}/incidents/{out_low.incident_id}/close"
    )
    assert res_close.status_code == 200
    assert res_close.json()["status"] == "closed"

    # 6. Test get incident
    res_get = await client.get(f"/api/projects/{project_id}/incidents/{out_crit1.incident_id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == out_crit1.incident_id

    # 7. Test get 404 for unknown incident
    res_404 = await client.get(f"/api/projects/{project_id}/incidents/inc_unknown")
    assert res_404.status_code == 404
    assert res_404.json()["code"] == "incident_not_found"

    # 8. Test signals route
    res_sigs = await client.get(
        f"/api/projects/{project_id}/incidents/{out_crit1.incident_id}/signals"
    )
    assert res_sigs.status_code == 200
    sigs_data = res_sigs.json()
    assert len(sigs_data) == 1
    assert sigs_data[0]["id"] == out_crit1.signal_id

    res_sigs_404 = await client.get(f"/api/projects/{project_id}/incidents/inc_unknown/signals")
    assert res_sigs_404.status_code == 404
    assert res_sigs_404.json()["code"] == "incident_not_found"

    # 9. Test timeline route: returns only events with subject_id == incident_id, newest first, paged with before
    # Let's record additional events for out_crit1
    await event_log.record(
        project_id,
        source="agent",
        type="incident.updated",
        payload={"note": "investigating"},
        subject_id=out_crit1.incident_id,
    )
    await event_log.record(
        project_id,
        source="system",
        type="signal.raised",
        payload={"signal_id": "sig_other"},
        subject_id="inc_other_subject",
    )

    res_timeline = await client.get(
        f"/api/projects/{project_id}/incidents/{out_crit1.incident_id}/timeline"
    )
    assert res_timeline.status_code == 200
    timeline = res_timeline.json()
    assert len(timeline) >= 2
    # Check all belong to out_crit1.incident_id
    assert all(e["subject_id"] == out_crit1.incident_id for e in timeline)
    # Check newest first (seq descending)
    assert timeline[0]["seq"] > timeline[1]["seq"]

    # Test timeline with before
    before_seq = timeline[0]["seq"]
    res_before = await client.get(
        f"/api/projects/{project_id}/incidents/{out_crit1.incident_id}/timeline?before={before_seq}&limit=1"
    )
    assert res_before.status_code == 200
    timeline_before = res_before.json()
    assert len(timeline_before) == 1
    assert timeline_before[0]["seq"] < before_seq


async def test_acknowledge_and_close_routes_refuse_a_closed_incident(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)
    out = await services.incidents.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="table_closed",
            model=None,
            ran_at=t0,
            detail={},
        ),
        seen_at=t0,
    )
    base = f"/api/projects/{project_id}/incidents/{out.incident_id}"
    assert (await client.post(f"{base}/close")).status_code == 200

    for action in ("acknowledge", "close"):
        res = await client.post(f"{base}/{action}")
        assert res.status_code == 409
        assert res.json()["code"] == "incident_not_live"

    res_get = await client.get(base)
    assert res_get.json()["status"] == "closed"
