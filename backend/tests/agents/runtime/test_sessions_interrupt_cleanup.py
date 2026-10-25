"""The interrupted turn's cleanup runs whole, wherever the worker's cancel lands."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HangingObserver,
    HeldObserver,
    RecordingObserver,
    WorkerCapturingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env
from tests.agents.runtime.session_rows import create_exchange, message_row
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def test_a_cancel_during_the_running_write_interrupts_the_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_entered = asyncio.Event()
    release_write = asyncio.Event()
    workers: list[asyncio.Task[object]] = []
    running_writes = 0
    set_turn = SessionStore.set_turn

    @shielded
    async def held_write(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None
    ) -> None:
        write_entered.set()
        await release_write.wait()
        await set_turn(self, project_id, turn_id, status, error=error)

    async def holding_set_turn(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        # The second turn's "running" write is held, as a write the cancel lands in.
        nonlocal running_writes
        if status == "running":
            running_writes += 1
            if running_writes == 2:
                task = asyncio.current_task()
                assert task is not None
                workers.append(task)
                await held_write(self, project_id, turn_id, status, error=error)
                return
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", holding_set_turn)
    held = HeldObserver()
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
    await create_exchange(databases, env.project_id, "xch_01")
    # The first turn is held, so the posted message waits for the second turn: queued.
    await manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    posted = await manager.post(
        env.project_id,
        AGENT,
        author_name="Data Quality",
        from_agent="data-quality",
        text="Please look at the readme.",
        exchange_id="xch_01",
        hop=1,
    )
    assert posted.status == "queued"
    held.release.set()
    await asyncio.wait_for(write_entered.wait(), timeout=10)

    [worker] = workers
    worker.cancel()
    release_write.set()
    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)
    await asyncio.wait_for(manager.close(), timeout=10)

    row = await message_row(databases, env.project_id, posted.id)
    assert row["status"] == "complete"
    assert row["read_by_turn"] is None
    assert await turn_statuses(databases, env.project_id) == ["done", "interrupted"]
    # The second turn ends reported, though its start never was.
    assert [call[0] for call in after.calls] == ["started", "ended", "ended"]
    assert after.calls[2][4] == "interrupted"


async def test_a_second_cancel_during_the_interrupted_cleanup_still_reports_the_end(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    mark_interrupted = SessionStore.mark_interrupted

    @shielded
    async def held_mark_interrupted(self: SessionStore, project_id: str, turn_id: str) -> None:
        cleanup_entered.set()
        await release_cleanup.wait()
        await mark_interrupted(self, project_id, turn_id)

    monkeypatch.setattr(SessionStore, "mark_interrupted", held_mark_interrupted)
    capturing = WorkerCapturingObserver()
    hanging = HangingObserver(hang_in="started")
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[capturing, hanging],
    )
    manager = env.manager
    await manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(hanging.hanging.wait(), timeout=5)

    closing = asyncio.create_task(manager.close())
    await asyncio.wait_for(cleanup_entered.wait(), timeout=10)
    # A second cancel of the worker, as close() racing close_project().
    [worker] = capturing.workers
    worker.cancel()
    release_cleanup.set()
    await asyncio.wait_for(closing, timeout=10)

    assert [call[0] for call in capturing.calls] == ["started", "ended"]
    assert capturing.calls[1][4] == "interrupted"
    assert await turn_statuses(databases, env.project_id) == ["interrupted"]
