"""A turn whose end cannot be stored ends failed, and the queue goes on."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HeldObserver,
    WorkerCapturingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def test_a_failing_end_write_ends_the_turn_failed_and_the_next_turn_runs(
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
    set_turn = SessionStore.set_turn
    failures = ["disk full"]

    async def set_turn_failing_once(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "done" and failures:
            raise RuntimeError(failures.pop())
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", set_turn_failing_once)
    observer = HeldObserver()
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
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync
    await manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(observer.entered.wait(), timeout=5)
    queued = await manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    observer.release.set()

    first = (await collect_until_turn_ended(sub, timeout=10))[-1]
    second = (await collect_until_turn_ended(sub, timeout=10))[-1]

    assert isinstance(first, TurnEnded) and isinstance(second, TurnEnded)
    # One turn.ended for each turn: the first failed, the second ran as usual.
    assert first.data.turn_id != second.data.turn_id
    assert (first.data.status, second.data.status) == ("failed", "done")
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)
    assert [(c[0], c[4] if c[0] == "ended" else None) for c in observer.calls] == [
        ("started", None),
        ("ended", "failed"),
        ("started", None),
        ("ended", "done"),
    ]
    # Row order is unspecified; which turn holds which status is checked above.
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "failed"]
    assert "Could not store how turn" in caplog.text
    assert "disk full" in caplog.text

    await manager.close()


async def test_a_failing_fallback_is_raised_once_not_retried(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_turn = SessionStore.set_turn
    end_writes: list[str] = []

    async def set_turn_failing_at_the_end(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status in ("done", "failed"):
            end_writes.append(status)
            raise RuntimeError(f"disk full writing {status}")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", set_turn_failing_at_the_end)
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
    await env.manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(observer.started.wait(), timeout=10)
    [worker] = observer.workers

    [outcome] = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)

    assert isinstance(outcome, RuntimeError)
    assert str(outcome) == "disk full writing failed"
    assert end_writes == ["done", "failed"]
    await env.manager.close()
