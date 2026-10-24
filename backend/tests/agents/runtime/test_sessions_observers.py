"""The turn observers the session manager calls when a turn starts and ends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    RecordingObserver,
    SelfCancellingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


async def test_an_observer_sees_a_turn_start_and_end_with_its_final_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    observer = RecordingObserver()
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
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    await manager.send(env.project_id, AGENT, "Check readme")
    events = await collect_until_turn_ended(sub)
    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    assert await asyncio.wait_for(observer.ended.get(), timeout=5) == "done"

    turn_id = ended.data.turn_id
    assert observer.calls == [
        ("started", env.project_id, AGENT, turn_id),
        ("ended", env.project_id, AGENT, turn_id, "done", "Done. First line: 1\tLine 1 of readme"),
    ]

    await manager.close()


async def test_an_observer_sees_a_stopped_turn_end(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    observer = RecordingObserver()
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
    manager = env.manager
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync

    await manager.send(env.project_id, AGENT, "Slow response")
    await through_first_delta(sub)
    await manager.stop(env.project_id, AGENT)
    await collect_until_turn_ended(sub)

    assert await asyncio.wait_for(observer.ended.get(), timeout=5) == "stopped"
    assert [call[0] for call in observer.calls] == ["started", "ended"]

    fake_openai.state.release_slow()
    await manager.close()


async def test_a_failing_observer_leaves_the_turns_running(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = RecordingObserver(fail=True)
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[failing, after],
    )
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    await manager.send(env.project_id, AGENT, "First")
    first = (await collect_until_turn_ended(sub))[-1]
    await manager.send(env.project_id, AGENT, "Second")
    second = (await collect_until_turn_ended(sub))[-1]

    assert isinstance(first, TurnEnded) and first.data.status == "done"
    assert isinstance(second, TurnEnded) and second.data.status == "done"
    # The observer after the failing one is still called, for both turns.
    for _ in range(2):
        assert await asyncio.wait_for(after.ended.get(), timeout=5) == "done"
    assert [call[0] for call in after.calls] == ["started", "ended", "started", "ended"]
    assert "A turn observer failed" in caplog.text

    await manager.close()


async def test_an_observers_own_cancel_leaves_the_turns_running(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancelling = SelfCancellingObserver()
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[cancelling, after],
    )
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    await manager.send(env.project_id, AGENT, "First")
    queued = await manager.send(env.project_id, AGENT, "Second")
    first = (await collect_until_turn_ended(sub))[-1]
    second = (await collect_until_turn_ended(sub))[-1]

    assert queued.status == "queued"
    assert isinstance(first, TurnEnded) and first.data.status == "done"
    assert isinstance(second, TurnEnded) and second.data.status == "done"
    for _ in range(2):
        assert await asyncio.wait_for(after.ended.get(), timeout=5) == "done"
    assert [call[0] for call in cancelling.calls] == ["started", "ended", "started", "ended"]
    assert await turn_statuses(databases, env.project_id) == ["done", "done"]
    assert "A turn observer was cancelled" in caplog.text

    await manager.close()
