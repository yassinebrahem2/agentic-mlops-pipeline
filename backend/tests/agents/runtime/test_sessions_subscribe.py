from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.hub_spy import watch_hub_subscriptions
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_closing_a_subscription_closes_the_hub_stream_at_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    closed = watch_hub_subscriptions(monkeypatch)

    subscription = env.manager.subscribe(env.project_id, "orchestrator", after_seq=None)
    await anext(subscription)
    assert not closed.is_set()

    await subscription.aclose()

    assert closed.is_set()
