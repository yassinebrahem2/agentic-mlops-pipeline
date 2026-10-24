"""Turn observers when a cancel lands in the end report, or the turn task is cancelled."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HeldEndObserver,
    RecordingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def test_a_cancel_during_the_end_report_still_reaches_every_observer(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    held = HeldEndObserver()
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[held, after],
    )
    manager = env.manager
    await manager.send(env.project_id, AGENT, "Check readme")
    queued = await manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    await asyncio.wait_for(held.entered.wait(), timeout=10)

    closing = asyncio.create_task(manager.close())
    # One pass of the loop, not a delay: close() cancels the workers before its first await.
    await asyncio.sleep(0)
    held.release.set()
    await asyncio.wait_for(closing, timeout=10)

    # The observer after the held one still hears the first turn end...
    assert [call[0] for call in after.calls] == ["started", "ended"]
    assert after.calls[1][4] == "done"
    # ...and the cancel still stops the worker: the queued turn never starts.
    assert [call[0] for call in held.calls] == ["started", "ended"]
    assert await turn_statuses(databases, env.project_id) == ["done", "queued"]
