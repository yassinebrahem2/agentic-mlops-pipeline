"""A message taken mid-turn and delivered again once that turn does not end done (#569): one
owner starts the worker for its new queued turn, only when none runs, and a Stop that comes
before that worker takes the turn is between two turns, and dropped."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langgraph.types import interrupt

from mlops_agents.agents.runtime.contract import TurnEnded, TurnWaiting
from mlops_agents.agents.runtime.hooks import Gate, GateCall, GateDecision
from mlops_agents.agents.runtime.model_caller import TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.worker import TurnWorkers
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.messaging.bus_env import BusEnv
from tests.agents.messaging.test_inbound import (
    HoldingGate,
    build_inbound_env,
    collect_until_turn_waiting,
)
from tests.agents.repair_doubles import TurnCounter
from tests.agents.runtime.hook_fakes import ALLOW
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.agents.turn_end_doubles import hold
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "beta"
TAKEN = "Crucial update."


class AskOnSecondStepGate(Gate):
    """The turn's first step is held until released, then allowed; its second asks for
    approval, so the turn waits; every later step is allowed."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._steps = 0

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        self._steps += 1
        if self._steps == 1:
            self.entered.set()
            await self.release.wait()
        if self._steps == 2:
            return {
                c.tool_call_id: GateDecision(
                    outcome="ask", message=None, approval_id=f"apr_{c.tool_call_id}"
                )
                for c in calls
            }
        return {c.tool_call_id: ALLOW for c in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        if any(d.outcome == "ask" for d in decisions.values()):
            interrupt(None)
        return {cid: ALLOW for cid in decisions}


class WorkerStarts:
    """Each worker `TurnWorkers.start` starts, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workers: list[asyncio.Task[None]] = []
        original = TurnWorkers.start
        workers = self.workers

        def start(self: TurnWorkers, project_id: str, session_id: str, *a: Any, **k: Any) -> None:
            original(self, project_id, session_id, *a, **k)
            worker = self.get((project_id, session_id))
            assert worker is not None
            workers.append(worker)

        monkeypatch.setattr(TurnWorkers, "start", start)

    def is_worker(self) -> bool:
        """Whether the calling task is a worker started from here on."""
        return asyncio.current_task() in self.workers


def _ends(turns: TurnCounter, turn_id: str) -> list[str]:
    return [status for t, status in turns.ended if t == turn_id]


def _asked_with_taken(fake: RunningFake, start: int) -> bool:
    """Whether a model request from the `start`th on carries the message taken mid-turn."""
    return any(
        "Message from alpha:" in json.dumps(r["messages"]) and TAKEN in json.dumps(r["messages"])
        for r in fake.state.requests[start:]
    )


async def _waiting_with_a_taken_message(env: BusEnv, gate: AskOnSecondStepGate) -> tuple[str, str]:
    """Beta's turn takes alpha's message mid-turn, then waits for approval: (session, turn)."""
    project_id, manager = env.session.project_id, env.session.manager
    view = await manager.get(project_id, AGENT)
    sub = manager.subscribe(project_id, AGENT, after_seq=view.last_seq)
    await manager.send(project_id, AGENT, "Inspect the repository.")
    await gate.entered.wait()
    await env.bus.send(project_id, sender="alpha", sender_turn_id="trn_test", to=AGENT, text=TAKEN)
    gate.release.set()
    waited = await collect_until_turn_waiting(sub)
    await sub.aclose()
    turn_id = next(e.data.turn_id for e in waited if isinstance(e, TurnWaiting))
    stored = await env.session.store.messages(project_id, view.id)
    taken = [m for m in stored if m.from_agent == "alpha"]
    assert [m.status for m in taken] == ["complete"]
    return view.id, turn_id


async def test_a_second_stop_before_the_redelivered_turn_is_taken_leaves_that_turn_running(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported case (#569): Stop of the waiting turn delivers its taken message again,
    and a second Stop comes before the worker started for it takes its first look."""
    gate = AskOnSecondStepGate()
    turns = TurnCounter()
    env, _ = await build_inbound_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
        observers=[turns],
    )
    project_id, manager = env.session.project_id, env.session.manager

    try:
        async with asyncio.timeout(20):
            session_id, turn_id = await _waiting_with_a_taken_message(env, gate)
            starts = WorkerStarts(monkeypatch)
            first_look = hold(
                monkeypatch, SessionStore, "next_queued_turn", lambda *_a, **_k: starts.is_worker()
            )
            try:
                await manager.stop(project_id, AGENT)
                await first_look.wait_entered()
                redelivered = await env.session.store.next_queued_turn(project_id, session_id)
                assert redelivered is not None
                requests = len(fake_openai.state.requests)
                await manager.stop(project_id, AGENT)
                first_look.release.set()
                await turns.wait_ended(redelivered[0])
            finally:
                first_look.release.set()
    finally:
        await manager.close()

    assert _ends(turns, turn_id) == ["stopped"]
    assert _ends(turns, redelivered[0]) == ["done"]
    assert _asked_with_taken(fake_openai, requests)


async def test_a_stop_of_a_waiting_turn_whose_message_is_delivered_again_starts_one_worker(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redelivery starts the worker for the new queued turn; Stop's own look for queued
    turns then finds it running, and starts none."""
    gate = AskOnSecondStepGate()
    turns = TurnCounter()
    env, _ = await build_inbound_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
        observers=[turns],
    )
    project_id, manager = env.session.project_id, env.session.manager

    try:
        async with asyncio.timeout(20):
            session_id, turn_id = await _waiting_with_a_taken_message(env, gate)
            starts = WorkerStarts(monkeypatch)
            first_look = hold(
                monkeypatch, SessionStore, "next_queued_turn", lambda *_a, **_k: starts.is_worker()
            )
            try:
                await manager.stop(project_id, AGENT)
                await first_look.wait_entered()
                started = len(starts.workers)
                redelivered = await env.session.store.next_queued_turn(project_id, session_id)
                assert redelivered is not None
                first_look.release.set()
                await turns.wait_ended(redelivered[0])
            finally:
                first_look.release.set()
    finally:
        await manager.close()

    assert started == 1
    assert _ends(turns, turn_id) == ["stopped"]
    assert _ends(turns, redelivered[0]) == ["done"]


async def test_a_redelivery_while_the_worker_runs_starts_no_second_worker(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn takes a message, then fails while its worker runs: that worker goes on to the
    message's new queued turn, and the redelivery starts none."""
    gate = HoldingGate()
    turns = TurnCounter()
    env, _ = await build_inbound_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
        observers=[turns],
    )
    project_id, manager = env.session.project_id, env.session.manager
    caller: Any = getattr(manager, "_caller", None)
    assert caller is not None
    original_call = caller.call
    failed = False

    async def fails_once_released(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if not failed and gate.release.is_set():
            failed = True
            raise TurnFailed("Simulated failure after the take")
        return await original_call(*args, **kwargs)

    monkeypatch.setattr(caller, "call", fails_once_released)
    starts = WorkerStarts(monkeypatch)

    try:
        async with asyncio.timeout(20):
            view = await manager.get(project_id, AGENT)
            sub = manager.subscribe(project_id, AGENT, after_seq=view.last_seq)
            await manager.send(project_id, AGENT, "Inspect the repository.")
            await gate.entered.wait()
            await env.bus.send(
                project_id, sender="alpha", sender_turn_id="trn_test", to=AGENT, text=TAKEN
            )
            gate.release.set()
            first = await collect_until_turn_ended(sub, timeout=10)
            second = await collect_until_turn_ended(sub, timeout=10)
            await sub.aclose()
    finally:
        await manager.close()

    first_end = next(e for e in first if isinstance(e, TurnEnded)).data
    second_end = next(e for e in second if isinstance(e, TurnEnded)).data
    assert first_end.status == "failed"
    assert second_end.status == "done"
    assert second_end.turn_id != first_end.turn_id
    assert len(starts.workers) == 1
    assert _ends(turns, second_end.turn_id) == ["done"]
