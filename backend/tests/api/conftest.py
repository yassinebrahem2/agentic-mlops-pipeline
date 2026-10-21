import asyncio
import socket
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI

from mlops_agents.api.app import create_app
from mlops_agents.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SessionSigner
from mlops_agents.config import RunMode, load_settings
from mlops_agents.errors import DomainError
from tests.conftest import MemoryKeyStore

LOCAL_URL = "http://127.0.0.1:8765"
SERVER_URL = "https://app.example.com"
SERVER_ENV = {
    "MLOPS_AGENTS_PASSWORD": "test-password",
    "MLOPS_AGENTS_MASTER_KEY": "test-master-key",
    "MLOPS_AGENTS_ALLOWED_HOSTS": "app.example.com",
}

MakeClient = Callable[..., AbstractAsyncContextManager[httpx2.AsyncClient]]


class NoGh:
    """A GitHub CLI that is not installed (GhCli() would look gh up on PATH)."""

    @property
    def available(self) -> bool:
        return False

    async def token(self) -> str:
        raise DomainError(
            "gh_not_installed",
            "The GitHub CLI (gh) is not installed. Install it, or paste a token instead.",
            status=409,
        )


def signed_in(app: FastAPI, base_url: str) -> httpx2.AsyncClient:
    """A client for an in-process app holding a fresh session cookie and its CSRF token."""
    signer: SessionSigner = app.state.signer
    cookie, csrf = signer.issue()
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=base_url,
        cookies={SESSION_COOKIE: cookie, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
    )


@pytest.fixture
def make_client(tmp_path: Path) -> MakeClient:
    """`make_client(mode, **settings_env)`: a started app with its own data folder in `mode`,
    and a signed-in client for it (https://app.example.com in server mode)."""

    @asynccontextmanager
    async def make(
        mode: Literal["local", "server"] = "local", **settings_env: str
    ) -> AsyncGenerator[httpx2.AsyncClient]:
        home = tempfile.mkdtemp(dir=tmp_path)
        settings = load_settings(RunMode(mode), env={"MLOPS_AGENTS_HOME": home, **settings_env})
        app = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())
        async with app.router.lifespan_context(app):
            base_url = SERVER_URL if settings.mode is RunMode.SERVER else LOCAL_URL
            async with signed_in(app, base_url) as http:
                yield http

    return make


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The started local-mode app that `client` and `anonymous` call."""
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(tmp_path / "data")})
    application = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    cookie, csrf = app.state.signer.issue()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=LOCAL_URL,
        cookies={SESSION_COOKIE: cookie, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
    ) as http:
        yield http


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=LOCAL_URL
    ) as http:
        yield http


@pytest.fixture
async def server_client(make_client: MakeClient) -> AsyncIterator[httpx2.AsyncClient]:
    async with make_client("server", **SERVER_ENV) as http:
        yield http


@pytest.fixture
async def live_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The local-mode app served by uvicorn on a free 127.0.0.1 port (the one `live_client`
    calls). The port is picked first so that `settings.port` is the port it is served on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    settings = load_settings(
        RunMode.LOCAL, port=port, env={"MLOPS_AGENTS_HOME": str(tmp_path / "live")}
    )
    application = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())
    server = uvicorn.Server(
        uvicorn.Config(application, timeout_graceful_shutdown=5, log_config=None)
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.01)
        yield application
    finally:
        server.should_exit = True
        await serving
        sock.close()


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A signed-in client for `live_app` over a real socket, so it can read a stream."""
    cookie, csrf = live_app.state.signer.issue()
    async with httpx2.AsyncClient(
        base_url=f"http://127.0.0.1:{live_app.state.services.settings.port}",
        cookies={SESSION_COOKIE: cookie, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
    ) as http:
        yield http
