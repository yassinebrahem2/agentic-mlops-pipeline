import asyncio
import socket
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from sse_starlette.sse import AppStatus

from mlops_agents.api.app import create_app
from mlops_agents.api.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SessionSigner
from mlops_agents.config import DEFAULT_PORT, RunMode, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.sandbox.docker import DockerRunner
from tests.conftest import MemoryKeyStore, make_repo
from tests.sandbox.fakes import FakeDockerRunner

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


def build_app(
    home: Path,
    mode: RunMode = RunMode.LOCAL,
    *,
    port: int = DEFAULT_PORT,
    settings_env: Mapping[str, str] | None = None,
    docker_runner: DockerRunner | None = None,
) -> FastAPI:
    """The app every fixture tests: data folder `home`, a memory key store, no `gh`."""
    env = {"MLOPS_AGENTS_HOME": str(home), **(settings_env or {})}
    settings = load_settings(mode, port=port, env=env)
    runner = docker_runner if docker_runner is not None else FakeDockerRunner()
    return create_app(settings, keystore=MemoryKeyStore(), gh=NoGh(), docker_runner=runner)


@asynccontextmanager
async def started_app(
    home: Path,
    mode: RunMode = RunMode.LOCAL,
    settings_env: Mapping[str, str] | None = None,
    docker_runner: DockerRunner | None = None,
) -> AsyncGenerator[FastAPI]:
    """`build_app` with its lifespan running, called in process."""
    app = build_app(home, mode, settings_env=settings_env, docker_runner=docker_runner)
    async with app.router.lifespan_context(app):
        yield app


def signed_in(
    app: FastAPI,
    base_url: str,
    *,
    in_process: bool = True,
    timeout: httpx2.Timeout | float | None = None,
) -> httpx2.AsyncClient:
    """A client holding a fresh session cookie and its CSRF token. In process it calls `app`
    through ASGITransport; otherwise it calls `base_url` over a real socket."""
    signer: SessionSigner = app.state.signer
    cookie, csrf = signer.issue()
    client_kwargs: dict[str, Any] = {}
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app) if in_process else None,
        base_url=base_url,
        cookies={SESSION_COOKIE: cookie, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
        **client_kwargs,
    )


@pytest.fixture
def make_client(tmp_path: Path) -> MakeClient:
    """`make_client(mode, **settings_env)`: a started app with its own data folder in `mode`,
    and a signed-in client for it (https://app.example.com in server mode)."""

    @asynccontextmanager
    async def make(
        mode: Literal["local", "server"] = "local", **settings_env: str
    ) -> AsyncGenerator[httpx2.AsyncClient]:
        home = Path(tempfile.mkdtemp(dir=tmp_path))
        run_mode = RunMode(mode)
        base_url = SERVER_URL if run_mode is RunMode.SERVER else LOCAL_URL
        async with (
            started_app(home, run_mode, settings_env) as app,
            signed_in(app, base_url) as http,
        ):
            yield http

    return make


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The started local-mode app that `client` and `anonymous` call."""
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
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


@dataclass
class LiveServer:
    app: FastAPI
    server: uvicorn.Server
    serving: asyncio.Task[None]

    async def stop(self) -> None:
        self.server.should_exit = True
        AppStatus.should_exit = True
        try:
            await self.serving
        finally:
            AppStatus.should_exit = False


@asynccontextmanager
async def live_server(home: Path) -> AsyncGenerator[LiveServer]:
    """The local-mode app served by uvicorn on a free 127.0.0.1 port (the one `live_client`
    calls). The port is picked first so that `settings.port` is the port it is served on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        application = build_app(home, port=port)
        server = uvicorn.Server(
            uvicorn.Config(application, timeout_graceful_shutdown=5, log_config=None)
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        live = LiveServer(application, server, serving)
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(0.01)
            yield live
        finally:
            await live.stop()


@pytest.fixture
async def live_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with live_server(tmp_path / "live") as live:
        yield live.app


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A signed-in client for `live_app` over a real socket, so it can read a stream."""
    port = live_app.state.services.settings.port
    async with signed_in(
        live_app,
        f"http://127.0.0.1:{port}",
        in_process=False,
        timeout=httpx2.Timeout(20.0, connect=10.0),
    ) as http:
        yield http


async def add_local_project(app: FastAPI, tmp_path: Path, git_env: dict[str, str]) -> str:
    """A project on a fresh local repository; returns its id."""
    repo = make_repo(tmp_path / "test-repo", {"README.md": "# Test"}, git_env)
    return (await app.state.services.workspace.add_local(str(repo))).id
