from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI, Request

from mlops_agents.config import RunMode
from tests.api.conftest import LOCAL_URL, SERVER_ENV, SERVER_URL, started_app

BAD_HOST = {
    "code": "bad_host",
    "message": "This address is not allowed. Open the app at the address it printed.",
    "detail": None,
}
PROXY_ENV = {**SERVER_ENV, "MLOPS_AGENTS_TRUSTED_PROXIES": "10.0.0.1"}
FORWARDED = {"X-Forwarded-Host": "app.example.com", "X-Forwarded-Proto": "https"}


def _client(
    app: FastAPI,
    base_url: str = LOCAL_URL,
    *,
    peer: str = "127.0.0.1",
    headers: Mapping[str, str] | None = None,
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, client=(peer, 50000)),
        base_url=base_url,
        headers=headers,
    )


@pytest.fixture
async def server_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "server", RunMode.SERVER, PROXY_ENV) as application:
        yield application


@pytest.mark.parametrize("path", ["/api/auth/status", "/", "/projects/prj_1"])
async def test_an_unknown_host_is_400_on_every_path(app: FastAPI, path: str) -> None:
    async with _client(app, headers={"Host": "evil.example"}) as http:
        response = await http.get(path)

    assert response.status_code == 400
    assert response.json() == BAD_HOST


@pytest.mark.parametrize("host", ["127.0.0.1:9999", "localhost", "127.0.0.1", "app.example.com"])
async def test_local_mode_needs_its_own_port(app: FastAPI, host: str) -> None:
    async with _client(app, headers={"Host": host}) as http:
        response = await http.get("/api/auth/status")

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["LOCALHOST:8765", "localhost:8765", "127.0.0.1:8765"])
async def test_local_hosts_pass_in_any_case(app: FastAPI, host: str) -> None:
    async with _client(app, headers={"Host": host}) as http:
        response = await http.get("/api/auth/status")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "https://127.0.0.1:8765", "http://127.0.0.1:9999", "null"]
)
async def test_an_origin_of_another_site_or_scheme_is_403(app: FastAPI, origin: str) -> None:
    async with _client(app, headers={"Origin": origin}) as http:
        response = await http.post("/api/auth/token", json={"token": "x"})

    assert response.status_code == 403
    assert response.json()["code"] == "bad_origin"
    assert response.json()["message"].endswith("Open the app at the address it printed.")


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://LOCALHOST:8765"])
async def test_the_apps_own_origin_passes(app: FastAPI, origin: str) -> None:
    async with _client(app, headers={"Origin": origin}) as http:
        response = await http.get("/api/auth/status")

    assert response.status_code == 200


async def test_a_dev_origin_from_settings_passes(tmp_path: Path) -> None:
    env = {"MLOPS_AGENTS_DEV_ORIGINS": "http://localhost:5173"}
    async with (
        started_app(tmp_path / "dev", settings_env=env) as application,
        _client(application, headers={"Origin": "http://localhost:5173"}) as http,
    ):
        response = await http.get("/api/auth/status")

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["app.example.com", "APP.example.com:443"])
async def test_server_mode_allows_its_allowed_hosts(server_app: FastAPI, host: str) -> None:
    async with _client(server_app, SERVER_URL, headers={"Host": host}) as http:
        response = await http.get("/api/auth/status")

    assert response.status_code == 200


async def test_server_mode_expects_an_https_origin(server_app: FastAPI) -> None:
    async with _client(server_app, SERVER_URL) as http:
        good = await http.get("/api/auth/status", headers={"Origin": "https://app.example.com"})
        bad = await http.get("/api/auth/status", headers={"Origin": "http://app.example.com"})

    assert good.status_code == 200
    assert bad.status_code == 403


async def test_a_trusted_proxy_forwards_the_host_and_scheme(server_app: FastAPI) -> None:
    async def url(request: Request) -> dict[str, str]:
        return {"url": str(request.url)}

    server_app.add_api_route("/test/url", url)

    async with _client(
        server_app, "http://10.0.0.5:8765", peer="10.0.0.1", headers=FORWARDED
    ) as http:
        response = await http.get("/test/url")

    assert response.status_code == 200
    assert response.json() == {"url": "https://app.example.com/test/url"}


async def test_forwarded_headers_from_another_peer_are_ignored(server_app: FastAPI) -> None:
    async with _client(
        server_app, "http://10.0.0.5:8765", peer="10.0.0.2", headers=FORWARDED
    ) as http:
        response = await http.get("/api/auth/status")

    assert response.status_code == 400
    assert response.json() == BAD_HOST


async def test_forwarded_host_uses_the_last_value_from_trusted_proxy(
    server_app: FastAPI,
) -> None:
    async with _client(
        server_app,
        "http://10.0.0.5:8765",
        peer="10.0.0.1",
        headers={"X-Forwarded-Host": "evil.example, app.example.com", "X-Forwarded-Proto": "https"},
    ) as http:
        good = await http.get("/api/auth/status")

    async with _client(
        server_app,
        "http://10.0.0.5:8765",
        peer="10.0.0.1",
        headers={"X-Forwarded-Host": "app.example.com, evil.example", "X-Forwarded-Proto": "https"},
    ) as http:
        bad = await http.get("/api/auth/status")

    assert good.status_code == 200
    assert bad.status_code == 400
    assert bad.json() == BAD_HOST


async def test_forwarded_proto_uses_the_last_value_from_trusted_proxy(
    server_app: FastAPI,
) -> None:
    async def url(request: Request) -> dict[str, str]:
        return {"url": str(request.url)}

    server_app.add_api_route("/test/url-proto", url)

    async with _client(
        server_app,
        "http://10.0.0.5:8765",
        peer="10.0.0.1",
        headers={"X-Forwarded-Host": "app.example.com", "X-Forwarded-Proto": "http, https"},
    ) as http:
        r1 = await http.get("/test/url-proto")

    async with _client(
        server_app,
        "http://10.0.0.5:8765",
        peer="10.0.0.1",
        headers={"X-Forwarded-Host": "app.example.com", "X-Forwarded-Proto": "https, http"},
    ) as http:
        r2 = await http.get("/test/url-proto")

    assert r1.status_code == 200
    assert r1.json() == {"url": "https://app.example.com/test/url-proto"}
    assert r2.status_code == 200
    assert r2.json() == {"url": "http://app.example.com/test/url-proto"}
