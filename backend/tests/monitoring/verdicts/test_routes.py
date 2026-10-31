"""The verdict and miss routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.clock import to_iso
from mlops_agents.monitoring.incidents.contract import SignalDraft
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app

T0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)


async def _until_the_clock_passes(stamp: datetime) -> None:
    """Waits until the system clock, at the stored millisecond precision, is past `stamp`, so
    the next record is strictly newer even where the clock ticks coarsely (Windows)."""
    for _ in range(200):
        if to_iso(datetime.now(UTC)) > to_iso(stamp):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the system clock did not move past the last record")


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


async def _incident(services: Services, project_id: str, subject: str, *, resolved: bool) -> str:
    outcome = await services.incidents.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="model_performance",
            subject=subject,
            model="churn",
            ran_at=T0,
            detail={},
            cleared=resolved,
        ),
        seen_at=T0,
    )
    return outcome.incident_id


async def test_the_routes_need_a_session(app: FastAPI) -> None:
    base = "/api/projects/prj_dummy"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=LOCAL_URL
    ) as anonymous:
        assert (
            await anonymous.post(f"{base}/incidents/inc_1/verdict", json={"value": "real_issue"})
        ).status_code == 401
        assert (await anonymous.get(f"{base}/verdicts")).status_code == 401
        assert (
            await anonymous.post(
                f"{base}/misses",
                json={"description": "missed", "occurred_at": T0.isoformat()},
            )
        ).status_code == 401
        assert (await anonymous.get(f"{base}/misses")).status_code == 401


async def test_verdict_routes_record_list_and_refuse(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    resolved = await _incident(services, project_id, "scores_resolved", resolved=True)
    still_open = await _incident(services, project_id, "scores_open", resolved=False)
    base = f"/api/projects/{project_id}"

    res = await client.post(
        f"{base}/incidents/{resolved}/verdict", json={"value": "real_issue", "note": "seen it"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["incident_id"] == resolved
    assert body["source"] == "user"
    assert body["note"] == "seen it"
    incident = (await client.get(f"{base}/incidents/{resolved}")).json()
    assert incident["status"] == "closed"
    assert incident["verdict"]["value"] == "real_issue"
    await _until_the_clock_passes(datetime.fromisoformat(body["created"]))

    res = await client.post(
        f"{base}/incidents/{still_open}/verdict",
        json={"value": "wrong_type", "right_category": "drift"},
    )
    assert res.status_code == 200
    assert (await client.get(f"{base}/incidents/{still_open}")).json()["status"] == "open"

    for refused in (
        {"value": "wrong_type"},
        {"value": "real_issue", "right_category": "drift"},
    ):
        res = await client.post(f"{base}/incidents/{still_open}/verdict", json=refused)
        assert res.status_code == 422

    res = await client.post(f"{base}/incidents/inc_unknown/verdict", json={"value": "real_issue"})
    assert res.status_code == 404
    assert res.json()["code"] == "incident_not_found"

    listed = (await client.get(f"{base}/verdicts")).json()
    assert [v["incident_id"] for v in listed] == [still_open, resolved]
    # The answer to the POST is the verdict as stored, to the stored precision.
    assert listed[1] == body
    latest_created = listed[0]["created"]
    since = await client.get(f"{base}/verdicts", params={"since": latest_created})
    assert [v["incident_id"] for v in since.json()] == [still_open]


async def test_miss_routes_store_with_the_servers_clock_and_list(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    base = f"/api/projects/{project_id}/misses"
    occurred = T0 - timedelta(days=1)

    before = datetime.now(UTC) - timedelta(milliseconds=1)
    res = await client.post(
        base,
        json={
            "description": "Recall fell and nothing fired.",
            "category": "model_performance",
            "occurred_at": occurred.isoformat(),
        },
    )
    after = datetime.now(UTC)
    assert res.status_code == 204
    assert res.content == b""

    res = await client.post(
        base,
        json={
            "description": "Claims a time.",
            "occurred_at": occurred.isoformat(),
            "reported_at": "2020-01-01T00:00:00Z",
        },
    )
    assert res.status_code == 422
    for description in ("", "x" * 2001):
        res = await client.post(
            base, json={"description": description, "occurred_at": occurred.isoformat()}
        )
        assert res.status_code == 422

    await _until_the_clock_passes(after)
    second = await client.post(
        base, json={"description": "A second miss.", "occurred_at": occurred.isoformat()}
    )
    assert second.status_code == 204

    listed = (await client.get(base)).json()
    assert [m["description"] for m in listed] == [
        "A second miss.",
        "Recall fell and nothing fired.",
    ]
    first = listed[1]
    assert first["category"] == "model_performance"
    assert datetime.fromisoformat(first["occurred_at"]) == occurred
    assert before <= datetime.fromisoformat(first["reported_at"]) <= after

    since = await client.get(base, params={"since": listed[0]["reported_at"]})
    assert [m["description"] for m in since.json()] == ["A second miss."]
