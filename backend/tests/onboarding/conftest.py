from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx2
import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_mlflow import FakeMlflowState, RunningFakeMlflow, start_fake_mlflow
from tests.fakes.fake_openai import RunningFake
from tests.onboarding.handoff_env import (
    PROJECT,
    HandoffEnv,
    RecordingKickoff,
    add_project,
    build_handoff_env,
)


@pytest.fixture
async def env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> AsyncIterator[SessionEnv]:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    yield env
    await env.manager.close()


@pytest.fixture
def mlflow() -> Iterator[RunningFakeMlflow]:
    running = start_fake_mlflow(FakeMlflowState())
    yield running
    running.stop()


@pytest.fixture
async def henv(
    tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore
) -> AsyncIterator[HandoffEnv]:
    """The handoff kind's services on a project of its own, with a recording kickoff."""
    await add_project(databases, PROJECT)
    built = await build_handoff_env(
        tmp_path=tmp_path,
        paths=paths,
        databases=databases,
        keystore=keystore,
        project_id=PROJECT,
        kickoff=RecordingKickoff(),
    )
    yield built
    await built.close()
