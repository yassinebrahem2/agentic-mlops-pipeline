from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import Resync, TurnEnded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages, turns
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta


async def test_close_project_during_turn(
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
    manager = env.manager
    project_id = env.project_id
    prov_name = env.provider
    settings_svc = env.settings
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    fake_openai.state.hold_slow()
    # Reading the snapshot starts the subscription now, so the held first delta reaches it.
    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    assert isinstance(await anext(sub), Resync)
    await manager.send(project_id, "orchestrator", "Slow response")
    await through_first_delta(sub)

    # Close project
    await manager.close_project(project_id)

    # The subscriber stream ends cleanly without raising CancelledError
    stream_ended = False
    async for _ in sub:
        pass
    stream_ended = True
    assert stream_ended

    # The turn and its message were left interrupted, not running or streaming.
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        assert (await conn.execute(select(turns.c.status))).scalars().all() == ["interrupted"]
        assert (
            await conn.execute(select(messages.c.status).where(messages.c.author_kind == "agent"))
        ).scalar_one() == "interrupted"
    await databases.close_project(project_id)

    # Database connections closed, project dir can be removed on Windows
    proj_dir = paths.project_dir(project_id)
    shutil.rmtree(proj_dir)
    assert not proj_dir.exists()


async def test_close_during_turn_leaves_turn_and_message_interrupted(
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
    manager = env.manager
    project_id = env.project_id
    prov_name = env.provider
    settings_svc = env.settings
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    fake_openai.state.hold_slow()
    # Reading the snapshot starts the subscription now, so the held first delta reaches it.
    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    assert isinstance(await anext(sub), Resync)
    await manager.send(project_id, "orchestrator", "Turn 1 slow")
    m2 = await manager.send(project_id, "orchestrator", "Turn 2 queued")
    await through_first_delta(sub)

    await manager.close()

    # Check database: turn 1 is interrupted, turn 1's agent message is interrupted, turn 2 is queued
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        turn_statuses = dict(
            (await conn.execute(select(turns.c.trigger_message_id, turns.c.status))).all()
        )
        assert turn_statuses[m2.id] == "queued"
        assert sorted(turn_statuses.values()) == ["interrupted", "queued"]

        agent_msg_status = (
            await conn.execute(select(messages.c.status).where(messages.c.author_kind == "agent"))
        ).scalar_one()
        assert agent_msg_status == "interrupted"


async def test_stop_after_turn_ended_does_not_swallow_a_later_shutdown(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    orig_set_turn = SessionStore.set_turn
    turn_one_ended = asyncio.Event()
    release_worker = asyncio.Event()

    async def hold_worker_after_turn_one(self: SessionStore, *args: Any, **kwargs: Any) -> None:
        await orig_set_turn(self, *args, **kwargs)
        if args[2] == "done" and not turn_one_ended.is_set():
            turn_one_ended.set()
            await release_worker.wait()

    monkeypatch.setattr(SessionStore, "set_turn", hold_worker_after_turn_one)

    # Reading the snapshot starts the subscription now, so it sees every event of both turns.
    stream = manager.subscribe(project_id, "orchestrator", after_seq=None)
    assert isinstance(await anext(stream), Resync)
    await manager.send(project_id, "orchestrator", "Turn 1 fast")
    await turn_one_ended.wait()

    # The turn's child task has ended but the worker has not moved on: this Stop has no target.
    await env.settings.set_default_model("orchestrator", f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    await manager.send(project_id, "orchestrator", "Turn 2 slow")
    third = await manager.send(project_id, "orchestrator", "Turn 3 queued")
    await manager.stop(project_id, "orchestrator")
    release_worker.set()

    # Turn 1 streamed text of its own, so its end is read first: the next delta is turn 2's.
    first = (await collect_until_turn_ended(stream))[-1]
    assert isinstance(first, TurnEnded)
    assert first.data.status == "done"
    await through_first_delta(stream)
    await manager.close()

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        statuses = dict(
            (await conn.execute(select(turns.c.trigger_message_id, turns.c.status))).all()
        )
    assert statuses[third.id] == "queued"
    assert sorted(statuses.values()) == ["done", "interrupted", "queued"]
