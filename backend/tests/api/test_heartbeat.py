"""Tests for the unauthenticated GET /api/health/heartbeat route (Task 3)."""

from __future__ import annotations

import httpx2
import pytest
from fastapi import FastAPI

from tests.api.conftest import LOCAL_URL
from tests.api.test_hosts import BAD_HOST


async def test_heartbeat_answers_without_cookie(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.get("/api/health/heartbeat")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_heartbeat_answers_with_cookie(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/health/heartbeat")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_heartbeat_refuses_post_anonymous_and_signed_in(
    anonymous: httpx2.AsyncClient, client: httpx2.AsyncClient
) -> None:
    # Anonymous POST to heartbeat is not in _OPEN, so it refuses with 401 not_signed_in
    anon_resp = await anonymous.post("/api/health/heartbeat")
    assert anon_resp.status_code == 401
    assert anon_resp.json()["code"] == "not_signed_in"

    # Signed-in POST is refused with 405 Method Not Allowed (GET only)
    client_resp = await client.post("/api/health/heartbeat")
    assert client_resp.status_code == 405


async def test_heartbeat_refuses_foreign_host(app: FastAPI) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url=LOCAL_URL,
        headers={"Host": "evil.example"},
    ) as http:
        response = await http.get("/api/health/heartbeat")

    assert response.status_code == 400
    assert response.json() == BAD_HOST


@pytest.mark.parametrize(
    "guarded_path",
    [
        "/api/app",
        "/api/projects",
        "/api/connections",
        "/api/models",
    ],
)
async def test_every_other_api_path_still_needs_session(
    anonymous: httpx2.AsyncClient, guarded_path: str
) -> None:
    response = await anonymous.get(guarded_path)
    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"
