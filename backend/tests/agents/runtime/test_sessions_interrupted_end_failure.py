"""An interrupted turn whose end cannot be stored is still reported ended, and the queue goes on."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver, turn_statuses
from tests.agents.runtime.session_env import (
    build_session_env,
    opened_stream,
    turn_ended_statuses,
)
from tests.agents.runtime.turn_tasks import interrupt_the_first_of_two_turns
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


def fail_mark_interrupted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    mark_interrupted = SessionStore.mark_interrupted
    failures = ["database is locked"]

    async def failing_once(self: SessionStore, project_id: str, turn_id: str) -> None:
        if failures:
            raise RuntimeError(failures.pop())
        await mark_interrupted(self, project_id, turn_id)

    monkeypatch.setattr(SessionStore, "mark_interrupted", failing_once)


def fail_every_interrupted_write(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every write of the interrupted status fails, the fallback's too; the others go through."""
    set_turn = SessionStore.set_turn
    failed: list[str] = []

    async def failing_set_turn(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "interrupted":
            failed.append(turn_id)
            raise RuntimeError("database is locked")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", failing_set_turn)
    return failed


async def test_a_failing_interrupted_cleanup_still_records_the_end_and_the_next_turn_runs(
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
    fail_mark_interrupted_once(monkeypatch)
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
    assert "Could not store how turn" in caplog.text
    assert "database is locked" in caplog.text
    await env.manager.close()


async def test_a_failing_fallback_write_is_logged_and_the_end_still_reported(
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
    failed = fail_every_interrupted_write(monkeypatch)
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

    # The write and its one fallback both failed: the turn stays running in the database
    # until the next recovery, which marks it interrupted.
    first_turn = failed[0]
    assert failed == [first_turn, first_turn]
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "running"]
    assert "Could not record turn" in caplog.text
    await env.manager.close()
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "interrupted"]


async def test_a_failing_interrupted_cleanup_at_close_still_ends_the_worker_cancelled(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_mark_interrupted_once(monkeypatch)
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
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Slow response")
    await through_first_delta(sub)
    [worker] = observer.workers

    await asyncio.wait_for(env.manager.close(), timeout=10)
    fake_openai.state.release_slow()

    # The close's cancel ends the worker, not the storage error of its cleanup.
    assert worker.cancelled()
    assert await turn_ended_statuses(sub) == ["interrupted"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "interrupted")]
    assert await turn_statuses(databases, env.project_id) == ["interrupted"]
