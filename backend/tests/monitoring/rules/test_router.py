"""Tests for /api/projects/{project_id}/rules routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RuleSpec,
    RuleTerm,
)
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


def _sample_spec(
    name: str = "auto-rollback",
    why: str = "Rollback when drift is high",
    model: str | None = "churn",
    hook_id: str = "hok_test1",
    hook_kind: str = "rollback",
    per_incident: int = 1,
    per_day: int = 2,
) -> RuleSpec:
    return RuleSpec(
        name=name,
        why=why,
        model=model,
        terms=[
            RuleTerm(
                check_id="drift_check",
                model=model,
                when=Compare(op="gt", field="value", value=0.5),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id=hook_id,
        hook_kind=hook_kind,  # type: ignore[arg-type]
        limits=RuleLimits(per_incident=per_incident, per_day=per_day),
    )


@pytest.mark.asyncio
async def test_rules_router_lifecycle(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # 1. Store rules directly via services.rules
    spec1 = _sample_spec(name="rule-churn", model="churn")
    rule1 = await services.rules.store(project_id, spec1, proposal_id="prp_1")

    spec2 = _sample_spec(name="rule-fraud", model="fraud")
    rule2 = await services.rules.store(project_id, spec2, proposal_id="prp_2")

    # 2. GET /api/projects/{project_id}/rules (all)
    resp = await client.get(f"/api/projects/{project_id}/rules")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    ids = [r["id"] for r in data]
    assert rule1.id in ids
    assert rule2.id in ids

    # 3. GET /api/projects/{project_id}/rules?model=churn
    resp_churn = await client.get(f"/api/projects/{project_id}/rules?model=churn")
    assert resp_churn.status_code == 200
    data_churn = resp_churn.json()
    assert len(data_churn) == 1
    assert data_churn[0]["id"] == rule1.id
    assert data_churn[0]["spec"]["model"] == "churn"

    # 4. PUT /api/projects/{project_id}/rules/{rule_id}/enabled -> False
    resp_disable = await client.put(
        f"/api/projects/{project_id}/rules/{rule1.id}/enabled",
        json={"enabled": False},
    )
    assert resp_disable.status_code == 200
    data_disabled = resp_disable.json()
    assert data_disabled["id"] == rule1.id
    assert data_disabled["enabled"] is False

    # Check via GET that rule is disabled
    resp_verify = await client.get(f"/api/projects/{project_id}/rules")
    rule1_fetched = next(r for r in resp_verify.json() if r["id"] == rule1.id)
    assert rule1_fetched["enabled"] is False

    # 5. PUT /api/projects/{project_id}/rules/{rule_id}/enabled -> True
    resp_enable = await client.put(
        f"/api/projects/{project_id}/rules/{rule1.id}/enabled",
        json={"enabled": True},
    )
    assert resp_enable.status_code == 200
    assert resp_enable.json()["enabled"] is True

    # 6. PUT with non-existent rule -> 404
    resp_404 = await client.put(
        f"/api/projects/{project_id}/rules/rul_nonexistent/enabled",
        json={"enabled": False},
    )
    assert resp_404.status_code == 404
