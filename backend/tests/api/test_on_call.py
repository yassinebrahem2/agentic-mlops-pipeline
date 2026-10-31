"""Tests for on-call API routes and handoff seeding integration (Task 2 of Plan #472)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.proposals.contract import HandoffPayload
from tests.api.conftest import add_local_project
from tests.proposals.samples import handoff


def _valid_on_call_payload(
    name: str = "Alice",
    escalation_order: int = 1,
    start: str = "09:00",
    end: str = "17:00",
    timezone: str = "Europe/Paris",
) -> dict[str, Any]:
    return {
        "name": name,
        "contacts": [{"channel": "email", "address": "alice@acme.test"}],
        "hours": {
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "start": start,
            "end": end,
        },
        "timezone": timezone,
        "escalation_order": escalation_order,
    }


@pytest.mark.asyncio
async def test_anonymous_cannot_access_on_call(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/projects/prj_dummy/on-call")).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/on-call",
            json=[_valid_on_call_payload()],
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_on_call_unknown_project_returns_404(client: httpx2.AsyncClient) -> None:
    res_get = await client.get("/api/projects/prj_unknown/on-call")
    assert res_get.status_code == 404
    assert res_get.json()["code"] == "project_not_found"

    res_put = await client.put(
        "/api/projects/prj_unknown/on-call",
        json=[_valid_on_call_payload()],
    )
    assert res_put.status_code == 404
    assert res_put.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_on_call_empty_list_initially(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.get(f"/api/projects/{project_id}/on-call")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_on_call_put_and_get(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    payload = [
        _valid_on_call_payload("Bob", escalation_order=2),
        _valid_on_call_payload("Alice", escalation_order=1),
    ]

    put_res = await client.put(f"/api/projects/{project_id}/on-call", json=payload)
    assert put_res.status_code == 200
    put_data = put_res.json()
    assert len(put_data) == 2
    # Returned list matching what was stored
    assert put_data[0]["id"] == "bob"
    assert put_data[1]["id"] == "alice"

    get_res = await client.get(f"/api/projects/{project_id}/on-call")
    assert get_res.status_code == 200
    get_data = get_res.json()
    # Ordered by escalation_order ASC, then id ASC:
    # 1. Alice (escalation 1)
    # 2. Bob (escalation 2)
    assert len(get_data) == 2
    assert get_data[0]["id"] == "alice"
    assert get_data[0]["name"] == "Alice"
    assert get_data[1]["id"] == "bob"
    assert get_data[1]["name"] == "Bob"


@pytest.mark.asyncio
async def test_on_call_duplicate_names_get_suffixed_ids(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    payload = [
        _valid_on_call_payload("Alice", escalation_order=1),
        _valid_on_call_payload("Alice", escalation_order=2),
        _valid_on_call_payload("Alice", escalation_order=3),
    ]

    put_res = await client.put(f"/api/projects/{project_id}/on-call", json=payload)
    assert put_res.status_code == 200
    data = put_res.json()
    assert [p["id"] for p in data] == ["alice", "alice-2", "alice-3"]


@pytest.mark.asyncio
async def test_on_call_invalid_hours_gives_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # start == end is invalid in OnCallHours
    bad_payload = [_valid_on_call_payload(start="09:00", end="09:00")]
    res = await client.put(f"/api/projects/{project_id}/on-call", json=bad_payload)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_on_call_bad_timezone_gives_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    bad_payload = [_valid_on_call_payload(timezone="Mars/Base")]
    res = await client.put(f"/api/projects/{project_id}/on-call", json=bad_payload)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_handoffs_store_seeds_list_through_observer_with_slugs_and_order(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    payload_dict = handoff()
    payload_dict["operations"]["on_call"] = [
        _valid_on_call_payload("Primary Responder", escalation_order=1),
        _valid_on_call_payload("Secondary Responder", escalation_order=2),
    ]
    payload = HandoffPayload.model_validate(payload_dict)

    # Storing through handoffs triggers the HandoffObserver (OnCallSeeder)
    version = await app.state.services.handoffs.store(project_id, "prop_1", payload)
    assert version == 1

    res = await client.get(f"/api/projects/{project_id}/on-call")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert data[0]["id"] == "primary-responder"
    assert data[0]["name"] == "Primary Responder"
    assert data[0]["escalation_order"] == 1
    assert data[1]["id"] == "secondary-responder"
    assert data[1]["name"] == "Secondary Responder"
    assert data[1]["escalation_order"] == 2


@pytest.mark.asyncio
async def test_second_handoff_version_replaces_on_call(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # First handoff
    payload1_dict = handoff()
    payload1_dict["operations"]["on_call"] = [_valid_on_call_payload("Original Lead")]
    payload1 = HandoffPayload.model_validate(payload1_dict)
    await app.state.services.handoffs.store(project_id, "prop_1", payload1)

    # Verify first handoff seeded
    res1 = await client.get(f"/api/projects/{project_id}/on-call")
    assert [p["name"] for p in res1.json()] == ["Original Lead"]

    # Second handoff with different person
    payload2_dict = handoff()
    payload2_dict["operations"]["on_call"] = [_valid_on_call_payload("Replacement Lead")]
    payload2 = HandoffPayload.model_validate(payload2_dict)
    await app.state.services.handoffs.store(project_id, "prop_2", payload2)

    # Second handoff replaces the list
    res2 = await client.get(f"/api/projects/{project_id}/on-call")
    assert [p["name"] for p in res2.json()] == ["Replacement Lead"]
    assert [p["id"] for p in res2.json()] == ["replacement-lead"]


@pytest.mark.asyncio
async def test_route_edit_survives_restart_reconcile(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # 1. Handoff v1 stored
    payload_dict = handoff()
    payload_dict["operations"]["on_call"] = [_valid_on_call_payload("Handoff Seed")]
    payload = HandoffPayload.model_validate(payload_dict)
    await app.state.services.handoffs.store(project_id, "prop_1", payload)

    # 2. Route edit
    edit_payload = [_valid_on_call_payload("Route Edited Person")]
    put_res = await client.put(f"/api/projects/{project_id}/on-call", json=edit_payload)
    assert put_res.status_code == 200

    # 3. Simulate startup reconcile (call seeder reconcile)
    # The seeder was registered on handoffs and created during build_services
    # We can invoke reconcile directly or simulate startup
    services = app.state.services
    # Find seeder by looking through observers or we can instantiate one with the same services
    from mlops_agents.notifications.on_call import OnCallSeeder

    seeder = OnCallSeeder(services.on_call, services.handoffs, services.databases)
    await seeder.reconcile(project_id)

    # Route edit survived!
    res = await client.get(f"/api/projects/{project_id}/on-call")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["name"] == "Route Edited Person"
    assert data[0]["id"] == "route-edited-person"
