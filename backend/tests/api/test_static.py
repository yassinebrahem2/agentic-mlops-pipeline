from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

from mlops_agents.api.app import create_app
from mlops_agents.api.static import mount_ui
from mlops_agents.config import RunMode, load_settings
from tests.api.conftest import LOCAL_URL, NoGh, signed_in
from tests.conftest import MemoryKeyStore
from tests.sandbox.fakes import FakeDockerRunner


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "web"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text(
        "<!doctype html><html><body><h1>App</h1></body></html>", encoding="utf-8"
    )
    assets = directory / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log('mlops');", encoding="utf-8")
    return directory


@pytest.fixture
async def static_app(tmp_path: Path, web_dir: Path) -> AsyncIterator[FastAPI]:
    home = tmp_path / "data"
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(home)})
    application = create_app(
        settings,
        keystore=MemoryKeyStore(),
        gh=NoGh(),
        web_dir=web_dir,
        docker_runner=FakeDockerRunner(),
    )
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def anonymous(static_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=static_app), base_url=LOCAL_URL
    ) as http:
        yield http


async def test_assets_file_is_served_with_js_content_type(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.get("/assets/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers.get("content-type", "").lower()
    assert response.text == "console.log('mlops');"


async def test_deep_link_returns_index_html(anonymous: httpx2.AsyncClient) -> None:
    response = await anonymous.get("/projects/prj_x/agents")
    assert response.status_code == 200
    assert "<h1>App</h1>" in response.text


async def test_path_traversal_never_returns_file_outside_web_dir(
    static_app: FastAPI, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("super_secret_outside", encoding="utf-8")
    app_db = tmp_path / "app.db"
    app_db.write_text("sqlite_database_bytes", encoding="utf-8")

    # 1. Traversal using %2e%2e is not normalized client-side and triggers the 404 guard
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=static_app), base_url=LOCAL_URL
    ) as http:
        r1 = await http.get("/%2e%2e/app.db")
        r2 = await http.get("/assets/%2e%2e/%2e%2e/secret.txt")
    assert r1.status_code == 404
    assert r2.status_code == 404
    assert "sqlite_database_bytes" not in r1.text
    assert "super_secret_outside" not in r2.text

    # 2. Direct ASGI scope call with unnormalized raw traversal paths
    async def run_asgi(path: str) -> int:
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request"}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"127.0.0.1:8765")],
        }
        await static_app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        status = start["status"]
        assert isinstance(status, int)
        return status

    for path in ["/../app.db", "/assets/../../secret.txt"]:
        assert await run_asgi(path) == 404


async def test_wrong_method_on_api_route_returns_405(static_app: FastAPI) -> None:
    async with signed_in(static_app, LOCAL_URL) as client:
        response = await client.post("/api/auth/status")
    assert response.status_code == 405
    data = response.json()
    assert data["code"] == "http_405"


async def test_api_unknown_stays_json_404(static_app: FastAPI) -> None:
    async with signed_in(static_app, LOCAL_URL) as client:
        response = await client.get("/api/unknown")
    assert response.status_code == 404
    assert response.headers.get("content-type", "").startswith("application/json")
    data = response.json()
    assert data["code"] == "not_found"
    assert data["message"] == "Nothing exists at this address. Check the link."


async def test_without_index_html_not_built_page_is_returned(tmp_path: Path) -> None:
    empty_web = tmp_path / "empty_web"
    empty_web.mkdir(parents=True)
    home = tmp_path / "data"
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(home)})
    application = create_app(
        settings,
        keystore=MemoryKeyStore(),
        gh=NoGh(),
        web_dir=empty_web,
        docker_runner=FakeDockerRunner(),
    )

    async with (
        application.router.lifespan_context(application),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=application), base_url=LOCAL_URL
        ) as http,
    ):
        r1 = await http.get("/")
        r2 = await http.get("/projects/prj_x")

    assert r1.status_code == 200
    assert (
        "The UI is not built. Run npm run build in frontend/, or npm run dev for development."
        in r1.text
    )
    assert r2.status_code == 200
    assert (
        "The UI is not built. Run npm run build in frontend/, or npm run dev for development."
        in r2.text
    )


async def test_ui_paths_need_no_session(anonymous: httpx2.AsyncClient) -> None:
    for path in ["/", "/assets/app.js", "/projects/prj_1"]:
        response = await anonymous.get(path)
        assert response.status_code == 200
        assert response.status_code != 401


async def test_websocket_request_is_rejected_at_fallback(tmp_path: Path) -> None:
    app = FastAPI()
    mount_ui(app, tmp_path)

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/ws",
        "headers": [],
    }

    await app(scope, receive, send)
    assert len(sent) >= 1
    assert sent[0]["type"] == "websocket.close"
