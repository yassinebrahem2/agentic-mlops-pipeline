"""The baseline routes: an incident's links, the silent team alerts and a verdict on one."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.results.contract import ResultRecord, StoredResult, source_id
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app

SOURCE = source_id("file", "*.jsonl", "results_conn")


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


async def _team_fire(
    services: Services, project_id: str, key: str, subject: str, ran_at: datetime
) -> StoredResult:
    record = ResultRecord(
        check_id=f"team_{subject}",
        origin="team",
        ran_at=ran_at,
        fired=True,
        category="model_performance",
        subject=subject,
        model="churn",
        detail={},
        source_ref=f"team.jsonl:{key}",
    )
    [stored] = await services.results.ingest(project_id, SOURCE, [(key, record)], ran_at)
    await services.results.deliver_undelivered(project_id, ran_at)
    return stored


async def test_the_routes_need_a_session(app: FastAPI) -> None:
    base = "/api/projects/prj_dummy"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=LOCAL_URL
    ) as anonymous:
        assert (await anonymous.get(f"{base}/incidents/inc_1/baseline")).status_code == 401
        assert (await anonymous.get(f"{base}/baseline/silent-alerts")).status_code == 401
        res = await anonymous.post(
            f"{base}/baseline/silent-alerts/res_1/verdict", json={"value": "real_issue"}
        )
        assert res.status_code == 401


async def test_baseline_routes_list_links_and_silent_alerts_and_take_a_verdict(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services: Services = app.state.services
    base = f"/api/projects/{project_id}"
    now = datetime.now(UTC)
    linked = await _team_fire(services, project_id, "k1", "scores", now - timedelta(minutes=5))
    silent = await _team_fire(services, project_id, "k2", "features", now - timedelta(minutes=4))
    outcome = await services.incidents.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="model_performance",
            subject="scores",
            model="churn",
            ran_at=now,
            detail={},
        ),
        seen_at=now,
    )

    links = (await client.get(f"{base}/incidents/{outcome.incident_id}/baseline")).json()
    assert [(link["incident_id"], link["team_check_id"]) for link in links] == [
        (outcome.incident_id, linked.check_id)
    ]
    res = await client.get(f"{base}/incidents/inc_unknown/baseline")
    assert res.status_code == 404
    assert res.json()["code"] == "incident_not_found"

    listed = (await client.get(f"{base}/baseline/silent-alerts")).json()
    assert [(a["result_id"], a["verdict"]) for a in listed] == [(silent.id, None)]

    verdict_url = f"{base}/baseline/silent-alerts/{silent.id}/verdict"
    res = await client.post(verdict_url, json={"value": "real_issue"})
    assert res.status_code == 200
    assert res.json()["verdict"] == "real_issue"
    res = await client.post(verdict_url, json={"value": "not_an_issue"})
    assert res.status_code == 200
    listed = (await client.get(f"{base}/baseline/silent-alerts")).json()
    assert listed == [res.json()]
    assert listed[0]["verdict"] == "not_an_issue"

    for refused in ({"value": "wrong_type"}, {"value": "real_issue", "by": "agent"}, {}):
        assert (await client.post(verdict_url, json=refused)).status_code == 422
    res = await client.post(
        f"{base}/baseline/silent-alerts/{linked.id}/verdict", json={"value": "real_issue"}
    )
    assert res.status_code == 404
    assert res.json()["code"] == "silent_alert_not_found"
