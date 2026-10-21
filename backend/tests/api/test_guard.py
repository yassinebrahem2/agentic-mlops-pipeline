import asyncio
import os
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from mlops_agents.api.deps import ProjectDep
from mlops_agents.api.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_DAYS,
    SessionSigner,
    load_secret,
)
from mlops_agents.paths import DataPaths
from mlops_agents.workspace.models import Project

LOCAL_URL = "http://127.0.0.1:8765"


def _ping_routes(app: FastAPI) -> None:
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/api/test/ping", ping, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])


def _client(
    app: FastAPI, cookie: str | bytes | None, csrf: str | bytes | None
) -> httpx2.AsyncClient:
    """A client sending these raw header bytes (non-ASCII included) and nothing else."""
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        raw = cookie if isinstance(cookie, bytes) else cookie.encode()
        headers.append((b"cookie", SESSION_COOKIE.encode() + b"=" + raw))
    if csrf is not None:
        headers.append((CSRF_HEADER.encode(), csrf if isinstance(csrf, bytes) else csrf.encode()))
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=LOCAL_URL, headers=headers
    )


async def test_a_stream_sends_its_first_chunk_before_the_route_finishes(
    live_app: FastAPI, live_client: httpx2.AsyncClient
) -> None:
    release = asyncio.Event()

    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first\n"
            await release.wait()
            yield b"last\n"

        return StreamingResponse(chunks(), media_type="text/plain")

    live_app.add_api_route("/api/test/stream", stream)

    async with asyncio.timeout(5), live_client.stream("GET", "/api/test/stream") as response:
        assert response.status_code == 200
        received = response.aiter_bytes()
        assert await anext(received) == b"first\n"
        release.set()
        assert [chunk async for chunk in received] == [b"last\n"]


async def test_anonymous_request_to_a_guarded_route_is_401(
    app: FastAPI, anonymous: httpx2.AsyncClient
) -> None:
    _ping_routes(app)

    response = await anonymous.get("/api/test/ping")

    assert response.status_code == 401
    assert response.json() == {"code": "not_signed_in", "message": "Sign in again.", "detail": None}


async def test_non_ascii_cookie_or_csrf_header_is_refused_not_500(app: FastAPI) -> None:
    _ping_routes(app)
    cookie, csrf = app.state.signer.issue()

    async with _client(app, "é.ü.ß".encode(), None) as http:
        bad_cookie = await http.get("/api/test/ping")
    async with _client(app, cookie, "é-token".encode()) as http:
        bad_header = await http.post("/api/test/ping")
    async with _client(app, cookie, csrf.encode() + "é".encode()) as http:
        appended = await http.post("/api/test/ping")

    assert bad_cookie.status_code == 401
    assert bad_header.status_code == 403
    assert appended.status_code == 403


@pytest.mark.parametrize(
    "cookie", ["", "abc", "a.b", "a.b.c.d", "1.nonce.forged-mac", "notanumber.nonce.mac"]
)
async def test_forged_or_malformed_session_cookie_is_401(app: FastAPI, cookie: str) -> None:
    _ping_routes(app)

    async with _client(app, cookie, None) as http:
        response = await http.get("/api/test/ping")

    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"


async def test_cookie_signed_with_another_secret_is_401(app: FastAPI) -> None:
    _ping_routes(app)
    cookie, csrf = SessionSigner(b"x" * 32).issue()

    async with _client(app, cookie, csrf) as http:
        response = await http.get("/api/test/ping")

    assert response.status_code == 401


async def test_expired_session_cookie_is_401(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    _ping_routes(app)
    issued_at = time.time() - SESSION_DAYS * 86400 - 60
    monkeypatch.setattr(time, "time", lambda: issued_at)
    cookie, csrf = app.state.signer.issue()
    monkeypatch.undo()

    async with _client(app, cookie, csrf) as http:
        response = await http.get("/api/test/ping")

    assert response.status_code == 401


async def test_state_changing_request_without_csrf_header_is_403(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    _ping_routes(app)
    del client.headers[CSRF_HEADER]

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await client.request(method, "/api/test/ping")
        assert response.status_code == 403
        assert response.json() == {
            "code": "csrf_failed",
            "message": "Reload the page and try again.",
            "detail": None,
        }


async def test_csrf_token_of_another_session_is_403(app: FastAPI) -> None:
    _ping_routes(app)
    cookie, _ = app.state.signer.issue()
    _, other_csrf = app.state.signer.issue()

    async with _client(app, cookie, other_csrf) as http:
        response = await http.post("/api/test/ping")

    assert response.status_code == 403


async def test_signed_in_client_with_its_token_passes(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    _ping_routes(app)

    assert (await client.get("/api/test/ping")).json() == {"ok": True}
    assert (await client.post("/api/test/ping")).json() == {"ok": True}
    del client.headers[CSRF_HEADER]
    assert (await client.get("/api/test/ping")).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/token"),
        ("POST", "/api/auth/login"),
        ("GET", "/"),
        ("GET", "/projects/prj_1/agents"),
    ],
)
async def test_sign_in_routes_and_ui_paths_pass_without_a_session(
    anonymous: httpx2.AsyncClient, method: str, path: str
) -> None:
    response = await anonymous.request(method, path)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_logout_needs_a_session_and_the_csrf_header(
    app: FastAPI, anonymous: httpx2.AsyncClient, client: httpx2.AsyncClient
) -> None:
    async def logout() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/api/auth/logout", logout, methods=["POST"])

    assert (await anonymous.post("/api/auth/logout")).status_code == 401
    assert (await client.post("/api/auth/logout")).status_code == 200
    del client.headers[CSRF_HEADER]
    assert (await client.post("/api/auth/logout")).status_code == 403


def test_load_secret_is_stable_and_private(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()

    first = load_secret(paths)
    second = load_secret(paths)

    assert first == second
    assert len(first) == 32
    if os.name != "nt":
        assert stat.S_IMODE(paths.cookie_secret.stat().st_mode) == 0o600


@pytest.mark.parametrize("project_id", ["prj_unknown", "NOTANID", "a..b"])
async def test_project_dep_answers_404_for_unknown_or_invalid_ids(
    app: FastAPI, client: httpx2.AsyncClient, project_id: str
) -> None:
    async def show(project: ProjectDep) -> Project:
        return project

    app.add_api_route("/api/test/projects/{project_id}", show)

    response = await client.get(f"/api/test/projects/{project_id}")

    assert response.status_code == 404
    assert response.json() == {
        "code": "project_not_found",
        "message": "This project does not exist.",
        "detail": None,
    }
