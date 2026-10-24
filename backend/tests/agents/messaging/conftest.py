from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.messaging.bus_env import BusEnv, build_bus_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


@pytest.fixture
async def bus_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> AsyncIterator[BusEnv]:
    env = await build_bus_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    yield env
    await env.session.manager.close()
