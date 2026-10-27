from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import Resync, TextPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


@pytest.mark.asyncio
async def test_session_manager_turn_states_stale_and_waiting(
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
    store = env.store

    # Ensure orchestrator session exists
    view = await manager.get(project_id, "orchestrator")
    session_id = view.id

    # Initially, no turns -> empty states
    assert await manager.turn_states(project_id) == {}

    # Insert a user message to act as trigger
    user_msg = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="trigger")],
        status="complete",
    )

    # 1. Stale running turn row with no worker (simulating crash): produces no entry
    turn_id = await store.add_turn(project_id, session_id, user_msg.id, "running")
    states = await manager.turn_states(project_id)
    assert states == {}
    assert "orchestrator" not in states

    # 2. A waiting turn row: produces "waiting"
    await store.set_turn(project_id, turn_id, "waiting")
    states = await manager.turn_states(project_id)
    assert states == {"orchestrator": "waiting"}

    # 3. Once resolved to done: produces no entry
    await store.set_turn(project_id, turn_id, "done")
    states = await manager.turn_states(project_id)
    assert states == {}

    await manager.close()


@pytest.mark.asyncio
async def test_session_manager_turn_states_running_worker(
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

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    assert isinstance(await anext(sub), Resync)

    # Start a slow turn
    await manager.send(project_id, "orchestrator", "Slow turn message")

    # While worker is running, turn_states reports "running"
    states = await manager.turn_states(project_id)
    assert states == {"orchestrator": "running"}

    # Release slow and let turn finish
    fake_openai.state.release_slow()
    await collect_until_turn_ended(sub)

    # After worker completes, turn_states reports empty
    states_done = await manager.turn_states(project_id)
    assert states_done == {}

    await manager.close()


async def test_turn_states_reports_no_running_turn_once_turn_ended_is_published(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker is held just before it exits, in its look for a next turn after the end: a
    worker that is only finishing its loop runs no turn (#273)."""
    looks: list[str] = []
    held = asyncio.Event()
    release = asyncio.Event()
    next_queued_turn = SessionStore.next_queued_turn

    async def held_after_the_first_turn(
        self: SessionStore, project_id: str, session_id: str
    ) -> tuple[str, str] | None:
        found = await next_queued_turn(self, project_id, session_id)
        looks.append("found" if found is not None else "none")
        if looks == ["found", "none"]:
            held.set()
            await release.wait()
        return found

    monkeypatch.setattr(SessionStore, "next_queued_turn", held_after_the_first_turn)
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, "orchestrator", "Check readme")
    await collect_until_turn_ended(sub, timeout=10)

    await asyncio.wait_for(held.wait(), timeout=10)
    assert await env.manager.turn_states(env.project_id) == {}

    release.set()
    await env.manager.close()
