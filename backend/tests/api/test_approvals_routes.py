"""Tests for approvals routes."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, ApprovalStatus
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.api.services import Services
from mlops_agents.storage.tables import messages, sessions, turns
from tests.agents.approval_env import seed_decided_approval
from tests.api.conftest import add_local_project


async def _setup_session_turn_and_approval(
    app: FastAPI,
    project_id: str,
    approval_id: str = "apr_api1",
    status: ApprovalStatus = "pending",
) -> str:
    services: Services = app.state.services
    databases = services.databases
    preview = Preview(kind="diff", title="Edit main.py", body="- old\n+ new")
    apr_part = ApprovalPart(
        type="approval",
        approval_id=approval_id,
        tool_call_id=f"call_{approval_id}",
        preview=preview,
        status=status,
        note=None,
    )
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                id="ses_1",
                agent="orchestrator",
                summary=None,
                compacted_through=None,
                created_at="2026-10-25T10:00:00Z",
            )
        )
        await conn.execute(
            turns.insert().values(
                id="trn_1",
                session_id="ses_1",
                trigger_message_id="msg_trigger",
                status="waiting",
                error=None,
                created_at="2026-10-25T10:00:00Z",
                started_at="2026-10-25T10:00:00Z",
                ended_at=None,
            )
        )
        await conn.execute(
            messages.insert().values(
                id="msg_1",
                session_id="ses_1",
                turn_id="trn_1",
                seq=1,
                author_kind="agent",
                author_name="orchestrator",
                parts=json.dumps([apr_part.model_dump()]),
                status="waiting",
                read_by_turn=None,
                from_agent=None,
                exchange_id=None,
                hop=None,
                created_at="2026-10-25T10:00:00Z",
            )
        )

    store = ApprovalStore(databases)
    if status != "pending":
        await seed_decided_approval(
            databases,
            store,
            project_id,
            status=status,
            id=approval_id,
            tool_call_id=f"call_{approval_id}",
            preview=preview,
        )
        return approval_id
    await store.create(
        project_id,
        id=approval_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id=f"call_{approval_id}",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
    )
    return approval_id


@pytest.mark.asyncio
async def test_anonymous_cannot_access_approvals(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/projects/prj_dummy/approvals")).status_code == 401
    assert (
        await anonymous.post(
            "/api/projects/prj_dummy/approvals/apr_1/decision",
            json={"approve": True},
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_approvals_unknown_project_returns_404(client: httpx2.AsyncClient) -> None:
    get_res = await client.get("/api/projects/prj_unknown/approvals")
    assert get_res.status_code == 404
    assert get_res.json()["code"] == "project_not_found"

    post_res = await client.post(
        "/api/projects/prj_unknown/approvals/apr_1/decision",
        json={"approve": True},
    )
    assert post_res.status_code == 404
    assert post_res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_approvals_list_pending(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Empty initially
    res = await client.get(f"/api/projects/{project_id}/approvals")
    assert res.status_code == 200
    assert res.json() == []

    # Add one pending approval
    apr_id = await _setup_session_turn_and_approval(app, project_id, "apr_list1")
    res = await client.get(f"/api/projects/{project_id}/approvals")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["id"] == apr_id
    assert data[0]["status"] == "pending"
    assert data[0]["tool"] == "edit_file"
    assert data[0]["preview"]["kind"] == "diff"


@pytest.mark.asyncio
async def test_approvals_decide_approve(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    apr_id = await _setup_session_turn_and_approval(app, project_id, "apr_dec1")

    res = await client.post(
        f"/api/projects/{project_id}/approvals/{apr_id}/decision",
        json={"approve": True, "note": "Approved by user"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == apr_id
    assert data["status"] == "approved"
    assert data["note"] == "Approved by user"
    assert data["decided_at"] is not None

    # Should no longer appear in pending
    list_res = await client.get(f"/api/projects/{project_id}/approvals")
    assert list_res.status_code == 200
    assert list_res.json() == []


@pytest.mark.asyncio
async def test_approvals_decide_reject(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    apr_id = await _setup_session_turn_and_approval(app, project_id, "apr_dec2")

    res = await client.post(
        f"/api/projects/{project_id}/approvals/{apr_id}/decision",
        json={"approve": False, "note": "Not now"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == apr_id
    assert data["status"] == "rejected"
    assert data["note"] == "Not now"


@pytest.mark.asyncio
async def test_approvals_decide_conflict_when_not_pending(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    apr_id = await _setup_session_turn_and_approval(app, project_id, "apr_dec3", status="approved")

    res = await client.post(
        f"/api/projects/{project_id}/approvals/{apr_id}/decision",
        json={"approve": True},
    )
    assert res.status_code == 409
    assert res.json()["code"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_approvals_decide_not_found(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.post(
        f"/api/projects/{project_id}/approvals/nonexistent_apr/decision",
        json={"approve": True},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "approval_not_found"


@pytest.mark.asyncio
async def test_approvals_decide_invalid_body_returns_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    apr_id = await _setup_session_turn_and_approval(app, project_id, "apr_dec4")

    # Missing required 'approve' field
    res = await client.post(
        f"/api/projects/{project_id}/approvals/{apr_id}/decision",
        json={"note": "hello"},
    )
    assert res.status_code == 422
