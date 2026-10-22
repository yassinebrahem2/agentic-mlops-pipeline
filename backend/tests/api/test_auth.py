import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.api.routers import auth
from mlops_agents.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SESSION_DAYS
from mlops_agents.config import RunMode
from tests.api.conftest import LOCAL_URL, SERVER_ENV, SERVER_URL, build_app, started_app

PASSWORD = SERVER_ENV["MLOPS_AGENTS_PASSWORD"]
BAD_TOKEN = {
    "code": "bad_token",
    "message": "This link is out of date. Open the address printed in the terminal where the app runs.",
    "detail": None,
}
BAD_PASSWORD = {"code": "bad_password", "message": "Wrong password.", "detail": None}
TOO_MANY = {
    "code": "too_many_attempts",
    "message": "Too many attempts. Wait a minute and try again.",
    "detail": None,
}


def _ping_route(app: FastAPI) -> None:
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/api/test/ping", ping)


def _anonymous(app: FastAPI, base_url: str, peer: str = "127.0.0.1") -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, client=(peer, 50000)), base_url=base_url
    )


def _set_cookies(response: httpx2.Response) -> dict[str, str]:
    """Each Set-Cookie header by cookie name."""
    return {value.split("=", 1)[0]: value for value in response.headers.get_list("set-cookie")}


def _flags(set_cookie: str) -> set[str]:
    return {part.strip().lower() for part in set_cookie.split(";")[1:]}


@pytest.fixture
async def server_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    env = {**SERVER_ENV, "MLOPS_AGENTS_TRUSTED_PROXIES": "10.0.0.1"}
    async with started_app(tmp_path / "server", RunMode.SERVER, env) as application:
        yield application


@pytest.fixture
async def visitor(server_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with _anonymous(server_app, SERVER_URL) as http:
        yield http


# Local mode


async def test_local_status_is_local_and_signed_out(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json() == {"mode": "local", "signed_in": False}


async def test_status_is_signed_in_with_a_session(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/auth/status")).json() == {"mode": "local", "signed_in": True}


async def test_a_wrong_token_is_401_without_a_cookie(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.post("/api/auth/token", json={"token": "wrong"})

    assert response.status_code == 401
    assert response.json() == BAD_TOKEN
    assert "set-cookie" not in response.headers


async def test_a_non_ascii_token_is_401_not_500(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.post("/api/auth/token", json={"token": "é-token"})

    assert response.status_code == 401


# A lone surrogate: valid in JSON and in a Python str, but not encodable as UTF-8.
LONE_SURROGATE_TOKEN = b'{"token": "\\ud800"}'
LONE_SURROGATE_PASSWORD = b'{"password": "\\ud800"}'
JSON_HEADERS = {"Content-Type": "application/json"}


async def test_a_lone_surrogate_token_is_the_wrong_token_401(
    anonymous: httpx2.AsyncClient,
) -> None:
    response = await anonymous.post(
        "/api/auth/token", content=LONE_SURROGATE_TOKEN, headers=JSON_HEADERS
    )

    assert response.status_code == 401
    assert response.json() == BAD_TOKEN
    assert "set-cookie" not in response.headers


async def test_the_right_token_signs_in_with_both_cookies(
    app: FastAPI, anonymous: httpx2.AsyncClient
) -> None:
    _ping_route(app)

    response = await anonymous.post("/api/auth/token", json={"token": app.state.access_token})

    assert response.status_code == 200
    assert response.json() == {"mode": "local", "signed_in": True}
    cookies = _set_cookies(response)
    common = {"samesite=strict", "path=/", f"max-age={SESSION_DAYS * 86400}"}
    assert _flags(cookies[SESSION_COOKIE]) == common | {"httponly"}
    assert _flags(cookies[CSRF_COOKIE]) == common
    assert (await anonymous.get("/api/test/ping")).json() == {"ok": True}
    assert (await anonymous.get("/api/auth/status")).json()["signed_in"] is True


async def test_login_is_not_available_in_local_mode(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.post("/api/auth/login", json={"password": "anything"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_available"


async def test_the_access_token_comes_from_settings_when_set(tmp_path: Path) -> None:
    app = build_app(tmp_path, settings_env={"MLOPS_AGENTS_ACCESS_TOKEN": "given-token"})

    assert app.state.access_token == "given-token"


def test_each_app_generates_its_own_token(tmp_path: Path) -> None:
    first = build_app(tmp_path / "a").state.access_token
    second = build_app(tmp_path / "b").state.access_token

    assert isinstance(first, str)
    assert len(first) >= 40
    assert first != second


# Server mode


async def test_server_status_is_server_and_signed_out(visitor: httpx2.AsyncClient) -> None:
    assert (await visitor.get("/api/auth/status")).json() == {"mode": "server", "signed_in": False}


async def test_a_wrong_password_is_401(visitor: httpx2.AsyncClient) -> None:
    response = await visitor.post("/api/auth/login", json={"password": "wrong"})

    assert response.status_code == 401
    assert response.json() == BAD_PASSWORD
    assert "set-cookie" not in response.headers


async def test_the_right_password_signs_in_with_secure_cookies(
    server_app: FastAPI, visitor: httpx2.AsyncClient
) -> None:
    _ping_route(server_app)

    response = await visitor.post("/api/auth/login", json={"password": PASSWORD})

    assert response.status_code == 200
    assert response.json() == {"mode": "server", "signed_in": True}
    cookies = _set_cookies(response)
    assert {"secure", "httponly", "samesite=strict"} <= _flags(cookies[SESSION_COOKIE])
    assert "secure" in _flags(cookies[CSRF_COOKIE])
    assert "httponly" not in _flags(cookies[CSRF_COOKIE])
    assert (await visitor.get("/api/test/ping")).json() == {"ok": True}


async def test_token_is_not_available_in_server_mode(
    server_app: FastAPI, visitor: httpx2.AsyncClient
) -> None:
    response = await visitor.post("/api/auth/token", json={"token": server_app.state.access_token})

    assert response.status_code == 404
    assert response.json()["code"] == "not_available"


async def test_the_sixth_attempt_is_429_even_with_the_right_password(
    visitor: httpx2.AsyncClient,
) -> None:
    for _ in range(5):
        assert (await visitor.post("/api/auth/login", json={"password": "x"})).status_code == 401

    response = await visitor.post("/api/auth/login", json={"password": PASSWORD})

    assert response.status_code == 429
    assert response.json() == TOO_MANY
    assert "set-cookie" not in response.headers


async def test_the_right_password_works_again_after_a_minute(
    visitor: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = auth.monotonic()
    monkeypatch.setattr(auth, "monotonic", lambda: start)
    for _ in range(5):
        await visitor.post("/api/auth/login", json={"password": "x"})
    monkeypatch.setattr(auth, "monotonic", lambda: start + 59)
    assert (await visitor.post("/api/auth/login", json={"password": PASSWORD})).status_code == 429

    monkeypatch.setattr(auth, "monotonic", lambda: start + 61)
    response = await visitor.post("/api/auth/login", json={"password": PASSWORD})

    assert response.status_code == 200


async def test_the_limit_is_per_client_address(server_app: FastAPI) -> None:
    async with _anonymous(server_app, SERVER_URL, peer="192.0.2.1") as first:
        for _ in range(5):
            await first.post("/api/auth/login", json={"password": "x"})
    async with _anonymous(server_app, SERVER_URL, peer="192.0.2.2") as second:
        response = await second.post("/api/auth/login", json={"password": PASSWORD})

    assert response.status_code == 200


async def test_behind_a_trusted_proxy_the_limit_uses_the_forwarded_address(
    server_app: FastAPI,
) -> None:
    async with _anonymous(server_app, SERVER_URL, peer="10.0.0.1") as proxy:
        for _ in range(5):
            await proxy.post(
                "/api/auth/login", json={"password": "x"}, headers={"X-Forwarded-For": "1.1.1.1"}
            )
        other = await proxy.post(
            "/api/auth/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"},
        )
        same = await proxy.post(
            "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "1.1.1.1"}
        )

    assert other.status_code == 200
    assert same.status_code == 429


async def test_forwarded_for_keys_on_rightmost_untrusted_hop(tmp_path: Path) -> None:
    env = {**SERVER_ENV, "MLOPS_AGENTS_TRUSTED_PROXIES": "10.0.0.1, 10.0.0.2"}
    async with (
        started_app(tmp_path / "proxy", RunMode.SERVER, env) as application,
        _anonymous(application, SERVER_URL, peer="10.0.0.2") as proxy,
    ):
        for _ in range(5):
            await proxy.post(
                "/api/auth/login",
                json={"password": "x"},
                headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.1"},
            )
        other = await proxy.post(
            "/api/auth/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-For": "3.3.3.3, 10.0.0.1"},
        )
        # Even with a forged hop prepended, the right-most untrusted hop is still 2.2.2.2 (10.0.0.1 is trusted proxy)
        blocked = await proxy.post(
            "/api/auth/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 10.0.0.1"},
        )

    assert other.status_code == 200
    assert blocked.status_code == 429


async def test_rotating_first_hop_does_not_escape_rate_limit(tmp_path: Path) -> None:
    env = {**SERVER_ENV, "MLOPS_AGENTS_TRUSTED_PROXIES": "10.0.0.2"}
    async with (
        started_app(tmp_path / "proxy2", RunMode.SERVER, env) as application,
        _anonymous(application, SERVER_URL, peer="10.0.0.2") as proxy,
    ):
        for _ in range(5):
            await proxy.post(
                "/api/auth/login",
                json={"password": "x"},
                headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
            )
        escaped_attempt = await proxy.post(
            "/api/auth/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-For": "9.9.9.9, 2.2.2.2"},
        )

    assert escaped_attempt.status_code == 429


async def test_a_lone_surrogate_password_is_a_failed_attempt(
    visitor: httpx2.AsyncClient,
) -> None:
    for _ in range(5):
        response = await visitor.post(
            "/api/auth/login", content=LONE_SURROGATE_PASSWORD, headers=JSON_HEADERS
        )
        assert response.status_code == 401
        assert response.json() == BAD_PASSWORD

    response = await visitor.post("/api/auth/login", json={"password": PASSWORD})

    assert response.status_code == 429


async def test_an_untrusted_peer_cannot_dodge_the_limit_with_forwarded_for(
    server_app: FastAPI,
) -> None:
    async with _anonymous(server_app, SERVER_URL, peer="192.0.2.9") as http:
        for hop in range(5):
            await http.post(
                "/api/auth/login",
                json={"password": "x"},
                headers={"X-Forwarded-For": f"1.1.1.{hop}"},
            )
        response = await http.post(
            "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "9.9.9.9"}
        )

    assert response.status_code == 429


# Logout


async def test_logout_needs_the_csrf_header(client: httpx2.AsyncClient) -> None:
    del client.headers[CSRF_HEADER]

    response = await client.post("/api/auth/logout")

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


async def test_logout_clears_both_cookies_and_ends_the_session(
    app: FastAPI, anonymous: httpx2.AsyncClient
) -> None:
    _ping_route(app)
    await anonymous.post("/api/auth/token", json={"token": app.state.access_token})
    anonymous.headers[CSRF_HEADER] = anonymous.cookies[CSRF_COOKIE]

    response = await anonymous.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"mode": "local", "signed_in": False}
    cookies = _set_cookies(response)
    assert set(cookies) == {SESSION_COOKIE, CSRF_COOKIE}
    assert all("max-age=0" in _flags(value) for value in cookies.values())
    assert SESSION_COOKIE not in anonymous.cookies
    assert (await anonymous.get("/api/test/ping")).status_code == 401


# Secrets


async def test_no_secret_reaches_a_response_or_a_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = "local-access-token-value"
    responses: list[httpx2.Response] = []
    async with (
        started_app(tmp_path / "local", settings_env={"MLOPS_AGENTS_ACCESS_TOKEN": token}) as local,
        _anonymous(local, LOCAL_URL) as http,
    ):
        responses.append(await http.post("/api/auth/token", json={"token": "wrong"}))
        responses.append(await http.post("/api/auth/token", json={"token": token}))
        responses.append(await http.post("/api/auth/login", json={"password": PASSWORD}))
        responses.append(await http.post("/api/auth/token", json={}))
        responses.append(await http.get("/api/auth/status"))
    async with (
        started_app(tmp_path / "server", RunMode.SERVER, SERVER_ENV) as server,
        _anonymous(server, SERVER_URL) as http,
    ):
        responses.append(await http.post("/api/auth/login", json={"password": "wrong"}))
        responses.append(await http.post("/api/auth/login", json={"password": PASSWORD}))
        responses.append(await http.post("/api/auth/token", json={"token": token}))
        responses.append(await http.post("/api/auth/login", json={"password": 1}))
        hashed = repr(server.state.password_hash)

    for response in responses:
        sent = response.text + repr(response.headers.multi_items())
        assert token not in sent
        assert PASSWORD not in sent
    assert token not in caplog.text
    assert PASSWORD not in caplog.text
    assert PASSWORD not in hashed
