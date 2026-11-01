from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings


class MemoryKeyStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.data.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.data[ref] = value

    def delete(self, ref: str) -> None:
        self.data.pop(ref, None)


class NoGh:
    @property
    def available(self) -> bool:
        return False

    async def token(self) -> str:
        raise RuntimeError("No gh")


AppMaker = Callable[[str], AbstractAsyncContextManager[tuple[FastAPI, str, Path]]]


@pytest.fixture
def app_factory(tmp_path: Path) -> AppMaker:
    @asynccontextmanager
    async def _make_app(
        token: str = "test-access-token-xyz",
    ) -> AsyncGenerator[tuple[FastAPI, str, Path]]:
        home = tmp_path / "app_home"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "MLOPS_AGENTS_HOME": str(home),
            "MLOPS_AGENTS_ACCESS_TOKEN": token,
        }
        settings = load_settings(RunMode.LOCAL, port=8765, env=env)
        app = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())
        async with app.router.lifespan_context(app):
            yield app, token, home

    return _make_app
