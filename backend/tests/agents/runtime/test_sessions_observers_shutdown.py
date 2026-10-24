"""Turn observers when close or close_project interrupts a turn."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime import worker
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HangingObserver,
    RecordingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


@pytest.mark.parametrize("closing", ["close", "close_project"])
async def test_closing_during_a_turn_reports_it_interrupted_once(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    closing: str,
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

    if closing == "close":
        await asyncio.wait_for(manager.close(), timeout=10)
    else:
        await asyncio.wait_for(manager.close_project(env.project_id), timeout=10)

    assert [call[0] for call in observer.calls] == ["started", "ended"]
    assert observer.calls[1][4] == "interrupted"
    assert observer.ended.qsize() == 1
    assert await turn_statuses(databases, env.project_id) == ["interrupted"]

    fake_openai.state.release_slow()
    await manager.close()


async def test_a_cancel_during_turn_started_reports_the_turn_interrupted(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    hanging = HangingObserver(hang_in="started")
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[hanging, after],
    )
    manager = env.manager
    await manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(hanging.hanging.wait(), timeout=5)

    await asyncio.wait_for(manager.close(), timeout=10)

    # The cancel landed in the first observer's turn_started: every observer hears the end.
    assert [call[0] for call in hanging.calls] == ["started", "ended"]
    assert hanging.calls[1][4] == "interrupted"
    assert [call[0] for call in after.calls] == ["ended"]
    assert after.calls[0][4] == "interrupted"
    assert await turn_statuses(databases, env.project_id) == ["interrupted"]


async def test_a_hanging_observer_does_not_hold_shutdown(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(worker, "OBSERVER_TIMEOUT_S", 0.2)
    hanging = HangingObserver(hang_in="ended")
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[hanging, after],
    )
    manager = env.manager
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync
    await manager.send(env.project_id, AGENT, "Slow response")
    await through_first_delta(sub)

    await asyncio.wait_for(manager.close(), timeout=10)

    assert hanging.calls[-1][4] == "interrupted"
    assert after.calls[-1][4] == "interrupted"
    assert "A turn observer failed" in caplog.text
    fake_openai.state.release_slow()
