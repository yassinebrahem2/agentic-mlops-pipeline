"""The session manager binds itself to the message bus."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class FakeBus:
    """Binds once, as MessageBus says; a second bind raises."""

    def __init__(self) -> None:
        self.sessions: AgentSessions | None = None

    async def send(
        self, project_id: str, *, sender: str, sender_turn_id: str, to: str, text: str
    ) -> None:
        raise AssertionError("not used here")

    def bind(self, sessions: AgentSessions) -> None:
        if self.sessions is not None:
            raise RuntimeError("The message bus is already bound.")
        self.sessions = sessions


async def test_the_session_manager_binds_itself_to_the_bus_once(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    bus = FakeBus()
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, bus=bus
    )

    assert bus.sessions is env.manager
    with pytest.raises(RuntimeError, match="already bound"):
        await build_session_env(
            tmp_path / "second",
            paths,
            databases,
            keystore,
            fake_openai,
            model_http,
            git_env,
            bus=bus,
        )

    await env.manager.close()
