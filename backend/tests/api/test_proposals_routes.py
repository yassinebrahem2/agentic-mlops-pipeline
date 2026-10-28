"""Tests for proposals routes: slot route, by-approval route, handoff route."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.agents.tools.base import Preview
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    ApplyResult,
    CredentialSlot,
    HandoffPayload,
    ProposalKind,
)
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.storage.tables import proposals as proposals_table
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


class MovingRouteTestKind(RouteTestKind):
    def __init__(self, secrets: ProjectSecrets) -> None:
        super().__init__()
        self.name = "moving_kind"
        self._secrets = secrets

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        src = proposal_slot_ref(project_id, proposal_id, 0)
        dst = f"project/{project_id}/moved_permanent"
        if await self._secrets.is_set(src):
            await self._secrets.move(src, dst)
        return ApplyResult(applied=["moved"], failed={})


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
    assert secret_val not in caplog.text

    # Secret value is absent from database row
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == view.id)))
            .mappings()
            .one()
        )
    assert secret_val not in str(row["payload"])
    if row["result"] is not None:
        assert secret_val not in str(row["result"])

    # Secret value is absent from event history
    events = await services.events.history(
        project_id, types=["proposal.created", "proposal.decided"]
    )
    for event in events:
        assert secret_val not in str(event.payload)

    # Keystore received the secret
    ref_0 = proposal_slot_ref(project_id, view.id, 0)
    assert await services.project_secrets.is_set(ref_0)
    assert services.project_secrets._keystore.get(ref_0) == secret_val


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
@pytest.mark.parametrize("status_name", ["approved", "rejected", "superseded"])
async def test_slot_route_non_pending_is_409(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    status_name: str,
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

    if status_name == "rejected":
        await services.proposals.decide(project_id, view.id, approved=False)
    elif status_name == "approved":
        await services.proposals.decide(project_id, view.id, approved=True)
    elif status_name == "superseded":
        await services.proposals.attach(project_id, view.id, "apr_1")
        v2 = await services.proposals.create(
            project_id,
            agent="architect",
            kind=kind.name,
            key="key1",
            payload={"connections": [connection()]},
            turn_id="trn_2",
            tool_call_id="call_2",
        )
        await services.proposals.attach(project_id, v2.id, "apr_2")

    resp = await client.put(
        f"/api/projects/{project_id}/proposals/{view.id}/credentials/0",
        json={"value": "foo"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "proposal_not_pending"

    # Assert keystore and is_set stay empty
    ref_0 = proposal_slot_ref(project_id, view.id, 0)
    assert not await services.project_secrets.is_set(ref_0)
    assert services.project_secrets._keystore.get(ref_0) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("moves_slot", [False, True])
async def test_slot_route_put_racing_with_decide(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    moves_slot: bool,
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind: ProposalKind = (
        MovingRouteTestKind(services.project_secrets) if moves_slot else RouteTestKind()
    )
    with contextlib.suppress(ValueError):
        services.proposal_kinds.register(kind)

    view = await services.proposals.create(
        project_id,
        agent="architect",
        kind=kind.name,
        key="race_key",
        payload={"connections": [connection()]},
        turn_id="trn_race",
        tool_call_id="call_race",
    )

    secret_val = "race_secret_value"
    ref_0 = proposal_slot_ref(project_id, view.id, 0)
    dst_ref = f"project/{project_id}/moved_permanent"

    async def do_put() -> httpx2.Response:
        return await client.put(
            f"/api/projects/{project_id}/proposals/{view.id}/credentials/0",
            json={"value": secret_val},
        )

    async def do_decide() -> ApplyResult | None:
        return await services.proposals.decide(project_id, view.id, approved=True)

    put_resp, _decide_res = await asyncio.gather(do_put(), do_decide())

    assert put_resp.status_code in (200, 409)
    if put_resp.status_code == 409:
        assert put_resp.json()["code"] == "proposal_not_pending"
        assert not await services.project_secrets.is_set(ref_0)
        assert services.project_secrets._keystore.get(ref_0) is None
        if moves_slot:
            assert services.project_secrets._keystore.get(dst_ref) is None
    else:
        assert put_resp.status_code == 200
        assert not await services.project_secrets.is_set(ref_0)
        assert services.project_secrets._keystore.get(ref_0) is None
        if moves_slot:
            assert services.project_secrets._keystore.get(dst_ref) == secret_val
        else:
            assert services.project_secrets._keystore.get(dst_ref) is None


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

    # Storing same proposal again returns same version and inserts nothing new
    v2_again = await services.handoffs.store(project_id, "prp_h2", p2)
    assert v2_again == v2

    resp3 = await client.get(f"/api/projects/{project_id}/handoff")
    assert resp3.status_code == 200
    d3 = resp3.json()
    assert d3["proposal_id"] == "prp_h2"
    assert d3["version"] == v2

    # History in handoff_versions has exactly 2 versions
    from mlops_agents.storage.tables import handoff_versions

    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(handoff_versions))).all()
    assert len(rows) == 2
