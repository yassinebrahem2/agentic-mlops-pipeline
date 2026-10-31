"""GET /api/projects/{id}/dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.proposals.payloads import HandoffPayload
from tests.api.conftest import add_local_project
from tests.proposals.samples import handoff


@pytest.mark.asyncio
async def test_anonymous_cannot_read_the_dashboard(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects/prj_dummy/dashboard")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_unknown_project_is_404(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_unknown/dashboard")
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_project_without_handoff_gets_an_empty_map(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    res = await client.get(f"/api/projects/{project_id}/dashboard")

    assert res.status_code == 200
    body = cast(dict[str, Any], res.json())
    assert body["stages"] == []
    assert body["edges"] == []
    assert body["models"] == []
    assert body["incidents_by_severity"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}
    # The default test app runs in local mode.
    assert body["heartbeat_reachable"] is False
    assert body["baseline_available"] is False


@pytest.mark.asyncio
async def test_dashboard_with_a_handoff_lists_stages_and_models(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    await app.state.services.handoffs.store(
        project_id, "prp_h1", HandoffPayload.model_validate(handoff())
    )

    res = await client.get(f"/api/projects/{project_id}/dashboard")

    assert res.status_code == 200
    body = cast(dict[str, Any], res.json())
    assert [s["name"] for s in body["stages"]] == ["prepare", "train"]
    assert body["edges"] == [{"from_stage": "prepare", "to_stage": "train"}]
    assert [m["name"] for m in body["models"]] == ["cancel", "noshow"]
