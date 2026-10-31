"""The stats route: a StatsView for the window and category asked, 422 on a bad one."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.stats.contract import STAT_METRICS
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


async def test_the_route_needs_a_session(app: FastAPI) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=LOCAL_URL
    ) as anonymous:
        assert (await anonymous.get("/api/projects/prj_dummy/stats")).status_code == 401


async def test_the_route_returns_the_stats_and_refuses_bad_values(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    now = services.clock.now()
    await services.incidents.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="drift",
            subject="features",
            ran_at=now,
            detail={},
        ),
        seen_at=now,
    )
    url = f"/api/projects/{project_id}/stats"

    body = (await client.get(url)).json()
    assert body["window"] == "30d"
    assert body["category"] is None
    assert body["baseline_available"] is False
    assert {g: list(m) for g, m in body["groups"].items()} == {
        g: list(names) for g, names in STAT_METRICS.items()
    }
    assert body["groups"]["noise"]["alerts_per_week"]["n"] == 1

    res = await client.get(url, params={"window": "7d", "category": "data_quality"})
    assert res.status_code == 200
    assert res.json()["window"] == "7d"
    assert res.json()["category"] == "data_quality"
    assert res.json()["groups"]["noise"]["alerts_per_week"]["n"] == 0

    for bad in ({"window": "90d"}, {"category": "accuracy"}):
        res = await client.get(url, params=bad)
        assert res.status_code == 422, bad
