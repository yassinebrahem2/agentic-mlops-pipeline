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
)
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.storage.tables import proposals as proposals_table
from tests.api.conftest import add_local_project
from tests.proposals.samples import connection, handoff


class RouteTestKind:
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(self) -> None:
        self.entered = asyncio.Event()  # set when apply starts
        self.release = asyncio.Event()  # apply waits on it once `gated`
        self.gated = False
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

    async def _hold(self) -> None:
        self.entered.set()
        if self.gated:
            await self.release.wait()

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        await self._hold()
        return ApplyResult(applied=["ok"], failed={})


class MovingRouteTestKind(RouteTestKind):
    def __init__(self, secrets: ProjectSecrets) -> None:
        super().__init__()
        self.name = "moving_kind"
        self._secrets = secrets

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        await self._hold()
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


async def _race_setup(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str], moves_slot: bool
) -> tuple[str, RouteTestKind, str]:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    kind: RouteTestKind = (
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
    return project_id, kind, view.id


@pytest.mark.asyncio
@pytest.mark.parametrize("moves_slot", [False, True])
async def test_slot_put_in_flight_before_apply_is_moved_or_swept(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    moves_slot: bool,
) -> None:
    project_id, kind, proposal_id = await _race_setup(app, tmp_path, git_env, moves_slot)
    secrets = app.state.services.project_secrets
    src = proposal_slot_ref(project_id, proposal_id, 0)
    dst = f"project/{project_id}/moved_permanent"
    secret_val = "race_secret_value"
    in_put = asyncio.Event()
    finish_put = asyncio.Event()
    real_put = secrets.put

    async def slow_put(ref: str, value: str) -> None:
        in_put.set()
        await finish_put.wait()
        await real_put(ref, value)

    secrets.put = slow_put  # the PUT is mid-write when decide starts

    put = asyncio.create_task(
        client.put(
            f"/api/projects/{project_id}/proposals/{proposal_id}/credentials/0",
            json={"value": secret_val},
        )
    )
    await in_put.wait()
    decide = asyncio.create_task(
        app.state.services.proposals.decide(project_id, proposal_id, approved=True)
    )
    await asyncio.sleep(0.05)
    assert not kind.entered.is_set()  # decide waits for the PUT's lock
    finish_put.set()
    put_resp = await put
    await decide

    assert put_resp.status_code == 200
    assert secret_val not in put_resp.text
    assert secrets._keystore.get(src) is None
    assert secrets._keystore.get(dst) == (secret_val if moves_slot else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("moves_slot", [False, True])
async def test_slot_put_during_apply_waits_and_gets_409(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    moves_slot: bool,
) -> None:
    project_id, kind, proposal_id = await _race_setup(app, tmp_path, git_env, moves_slot)
    secrets = app.state.services.project_secrets
    src = proposal_slot_ref(project_id, proposal_id, 0)
    dst = f"project/{project_id}/moved_permanent"
    kind.gated = True

    decide = asyncio.create_task(
        app.state.services.proposals.decide(project_id, proposal_id, approved=True)
    )
    await kind.entered.wait()
    put = asyncio.create_task(
        client.put(
            f"/api/projects/{project_id}/proposals/{proposal_id}/credentials/0",
            json={"value": "late_secret"},
        )
    )
    await asyncio.sleep(0.05)
    assert not put.done()  # the lock holds the PUT back while apply runs
    kind.release.set()
    await decide
    put_resp = await put

    assert put_resp.status_code == 409
    assert put_resp.json()["code"] == "proposal_not_pending"
    assert secrets._keystore.get(src) is None
    assert secrets._keystore.get(dst) is None


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
