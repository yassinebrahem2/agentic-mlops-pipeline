"""A cancel of the turn task by neither Stop nor a close ends that turn, and the queue goes on."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver, turn_statuses
from tests.agents.runtime.session_env import agent_message_statuses, build_session_env
from tests.agents.runtime.turn_tasks import interrupt_the_first_of_two_turns
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_an_outside_cancel_ends_the_turn_interrupted_and_the_next_turn_runs(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = WorkerCapturingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
    )

    await interrupt_the_first_of_two_turns(env, fake_openai, monkeypatch, observer)

    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "interrupted"]
    assert await agent_message_statuses(env) == ["interrupted", "complete"]
    await env.manager.close()
