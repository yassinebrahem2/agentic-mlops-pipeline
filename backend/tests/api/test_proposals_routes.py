"""Tests for proposals routes: slot route, by-approval route, handoff route."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.credentials.project_secrets import proposal_slot_ref
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    ApplyResult,
    CredentialSlot,
    HandoffPayload,
)
from mlops_agents.proposals.payloads import ConnectionsPayload
from tests.api.conftest import add_local_project
from tests.proposals.samples import connection, handoff


class RouteTestKind:
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(self) -> None:
        self.name = CONNECTIONS_KIND
        self._slots = [
            CredentialSlot(index=0, label="Slot 0", source_type="token"),
            CredentialSlot(index=1, label="Slot 1", source_type="password"),
        ]

    def key(self, payload: BaseModel) -> str:
        return "const_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Preview", body="preview body")

    def warnings(self, payload: BaseModel) -> list[str]:
        return ["Route warning"]

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return list(self._slots)

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        return ApplyResult(applied=["ok"], failed={})


@pytest.mark.asyncio
async def test_slot_route_writes_keystore_and_masks_value(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind = RouteTestKind()
    with contextlib.suppress(ValueError):
        services.proposal_kinds.register(kind)

    view = await services.proposals.create(
        project_id,
        agent="architect",
        kind=kind.name,
        key="key1",
        payload={"connections": [connection()]},
        turn_id="trn_1",
        tool_call_id="call_1",
    )

    secret_val = "super-secret-token-12345"
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            f"/api/projects/{project_id}/proposals/{view.id}/credentials/0",
            json={"value": secret_val},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["index"] == 0
    assert data["set"] is True
    # Secret value is absent from response
    assert secret_val not in resp.text
    # Secret value is absent from logs
    for record in caplog.records:
        assert secret_val not in record.message

    # Keystore received the secret
    ref_0 = proposal_slot_ref(project_id, view.id, 0)
    assert await services.project_secrets.is_set(ref_0)


@pytest.mark.asyncio
async def test_slot_route_unknown_proposal_or_slot_is_404(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind = RouteTestKind()
    with contextlib.suppress(ValueError):
        services.proposal_kinds.register(kind)

    view = await services.proposals.create(
        project_id,
        agent="architect",
        kind=kind.name,
        key="key1",
        payload={"connections": [connection()]},
        turn_id="trn_1",
        tool_call_id="call_1",
    )

    # Unknown proposal
    resp = await client.put(
        f"/api/projects/{project_id}/proposals/nonexistent/credentials/0",
        json={"value": "foo"},
    )
    assert resp.status_code == 404

    # Unknown slot
    resp2 = await client.put(
        f"/api/projects/{project_id}/proposals/{view.id}/credentials/99",
        json={"value": "foo"},
    )
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_slot_route_non_pending_is_409(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind = RouteTestKind()
    with contextlib.suppress(ValueError):
        services.proposal_kinds.register(kind)

    view = await services.proposals.create(
        project_id,
        agent="architect",
        kind=kind.name,
        key="key1",
        payload={"connections": [connection()]},
        turn_id="trn_1",
        tool_call_id="call_1",
    )

    # Reject it
    await services.proposals.decide(project_id, view.id, approved=False)

    resp = await client.put(
        f"/api/projects/{project_id}/proposals/{view.id}/credentials/0",
        json={"value": "foo"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "proposal_not_pending"


@pytest.mark.asyncio
async def test_by_approval_route(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind = RouteTestKind()
    with contextlib.suppress(ValueError):
        services.proposal_kinds.register(kind)

    # 404 for unknown approval
    resp_none = await client.get(f"/api/projects/{project_id}/proposals/by-approval/apr_unknown")
    assert resp_none.status_code == 404

    view = await services.proposals.create(
        project_id,
        agent="architect",
        kind=kind.name,
        key="key1",
        payload={"connections": [connection()]},
        turn_id="trn_1",
        tool_call_id="call_1",
    )
    await services.proposals.attach(project_id, view.id, "apr_known")

    resp = await client.get(f"/api/projects/{project_id}/proposals/by-approval/apr_known")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == view.id
    assert data["status"] == "pending"
    assert "slots" in data
    assert len(data["slots"]) == 2
    assert data["warnings"] == ["Route warning"]


@pytest.mark.asyncio
async def test_handoff_route(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # Initially null with 200 status
    resp0 = await client.get(f"/api/projects/{project_id}/handoff")
    assert resp0.status_code == 200
    assert resp0.json() is None

    # Store handoff 1
    p1 = HandoffPayload.model_validate(handoff())
    v1 = await services.handoffs.store(project_id, "prp_h1", p1)

    resp1 = await client.get(f"/api/projects/{project_id}/handoff")
    assert resp1.status_code == 200
    d1 = resp1.json()
    assert d1["proposal_id"] == "prp_h1"
    assert d1["version"] == v1

    # Store handoff 2
    p2 = HandoffPayload.model_validate(handoff())
    v2 = await services.handoffs.store(project_id, "prp_h2", p2)

    resp2 = await client.get(f"/api/projects/{project_id}/handoff")
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["proposal_id"] == "prp_h2"
    assert d2["version"] == v2
