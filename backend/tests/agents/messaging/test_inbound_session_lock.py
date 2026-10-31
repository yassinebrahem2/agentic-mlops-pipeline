"""The inbound source's mid-turn take and its redelivery run under the session's own lock and
publish to the session's own hub, both owned by the session registry (#573)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.messaging.inbound import SqliteInboundSource
from mlops_agents.agents.messaging.wakeups import WakeupService
from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageUpdated,
    MessageView,
    StreamEvent,
    TurnEnded,
)
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.api.services import Services
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.onboarding.kickoff import Kickoff
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.messaging.bus_env import BusEnv
from tests.agents.messaging.test_inbound import HoldingGate, build_inbound_env
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.agents.turn_end_doubles import hold
from tests.api.conftest import add_local_project, started_app
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "beta"
TAKEN = "Crucial update."


async def _start_turn_and_post(env: BusEnv, gate: HoldingGate) -> tuple[str, Any]:
    """Beta's turn is held in its first step while alpha posts to it: (session, live stream)."""
    project_id, manager = env.session.project_id, env.session.manager
    view = await manager.get(project_id, AGENT)
    sub = manager.subscribe(project_id, AGENT, after_seq=view.last_seq)
    await manager.send(project_id, AGENT, "Inspect the repository.")
    await gate.entered.wait()
    await env.bus.send(project_id, sender="alpha", sender_turn_id="trn_test", to=AGENT, text=TAKEN)
    return view.id, sub


async def _taken_id(env: BusEnv, session_id: str) -> str:
    stored = await env.session.store.messages(env.session.project_id, session_id)
    return next(m.id for m in stored if m.from_agent == "alpha")


def _updates(events: list[StreamEvent], message_id: str) -> list[str]:
    """The statuses the stream's `message.updated` events gave the message, in order."""
    return [
        e.data.message.status
        for e in events
        if isinstance(e, MessageUpdated) and e.data.message.id == message_id
    ]


def _fail_once_released(monkeypatch: pytest.MonkeyPatch, env: BusEnv, gate: HoldingGate) -> None:
    """The first model call once the gate is released fails: after that step's take."""
    caller: Any = getattr(env.session.manager, "_caller", None)
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


async def test_the_take_and_the_redelivery_reach_the_sessions_live_stream(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The take marks alpha's message complete while turn 1 runs; turn 1 then fails, and the
    redelivery queues it again: both changes reach a subscriber of beta's stream."""
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    _fail_once_released(monkeypatch, env, gate)
    manager = env.session.manager
    try:
        async with asyncio.timeout(20):
            session_id, sub = await _start_turn_and_post(env, gate)
            gate.release.set()
            first = await collect_until_turn_ended(sub, timeout=10)
            second = await collect_until_turn_ended(sub, timeout=10)
            await sub.aclose()
            taken_id = await _taken_id(env, session_id)
    finally:
        await manager.close()

    first_end = next(e for e in first if isinstance(e, TurnEnded)).data
    assert first_end.status == "failed"
    # The take, before turn 1 ended; the redelivery, after it (the next turn completes it).
    assert _updates(first, taken_id) == ["complete"]
    assert _updates(second, taken_id)[:1] == ["queued"]


async def test_a_send_waits_while_a_mid_turn_take_of_the_same_session_runs(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The take holds the session lock: a user's Send to beta, made while the take's write is
    held, waits for that lock, and its message comes after the take's update on the stream."""
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id, manager = env.session.project_id, env.session.manager
    senders: list[asyncio.Task[MessageView]] = []
    contended = asyncio.Event()
    added = asyncio.Event()

    acquire = AsyncRLock.acquire

    async def acquire_seen(self: AsyncRLock) -> None:
        current = asyncio.current_task()
        owner: Any = getattr(self, "_owner", None)
        if current in senders and owner is not None and owner is not current:
            contended.set()
        await acquire(self)

    add_message = SessionStore.add_message

    async def add_message_seen(self: SessionStore, *args: Any, **kwargs: Any) -> MessageView:
        if asyncio.current_task() in senders:
            added.set()
        return await add_message(self, *args, **kwargs)

    take = None
    try:
        async with asyncio.timeout(20):
            session_id, sub = await _start_turn_and_post(env, gate)
            take = hold(monkeypatch, SessionStore, "take_inbound")
            gate.release.set()
            await take.wait_entered()

            monkeypatch.setattr(AsyncRLock, "acquire", acquire_seen)
            monkeypatch.setattr(SessionStore, "add_message", add_message_seen)
            senders.append(asyncio.create_task(manager.send(project_id, AGENT, "From the user.")))
            seen = [asyncio.create_task(contended.wait()), asyncio.create_task(added.wait())]
            _, pending = await asyncio.wait(seen, return_when=asyncio.FIRST_COMPLETED)
            for waiter in pending:
                waiter.cancel()
            waited, wrote = contended.is_set(), added.is_set()

            take.release.set()
            user_message = await senders[0]
            events = await collect_until_turn_ended(sub, timeout=10)
            await sub.aclose()
            taken_id = await _taken_id(env, session_id)
    finally:
        if take is not None:
            take.release.set()
        await manager.close()

    assert waited
    assert not wrote
    taken_seq = next(
        e.seq for e in events if isinstance(e, MessageUpdated) and e.data.message.id == taken_id
    )
    user_seq = next(
        e.seq
        for e in events
        if isinstance(e, MessageCreated) and e.data.message.id == user_message.id
    )
    assert taken_seq < user_seq


async def test_a_mid_turn_take_in_a_real_turn_holds_the_registrys_lock_and_completes(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker, turn task and the graph's take: the take's write runs while the turn's own task
    holds the registry's lock for the session, and the turn ends done."""
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    manager = env.session.manager
    registry: Any = getattr(manager, "_registry", None)
    held_by_taker: list[bool] = []
    take_inbound = SessionStore.take_inbound

    async def take_inbound_seen(
        self: SessionStore, project: str, session: str, turn: str
    ) -> list[MessageView]:
        owner: Any = getattr(registry.lock(project, session), "_owner", None)
        held_by_taker.append(owner is asyncio.current_task())
        return await take_inbound(self, project, session, turn)

    try:
        async with asyncio.timeout(20):
            _, sub = await _start_turn_and_post(env, gate)
            monkeypatch.setattr(SessionStore, "take_inbound", take_inbound_seen)
            gate.release.set()
            events = await collect_until_turn_ended(sub, timeout=10)
            await sub.aclose()
    finally:
        await manager.close()

    assert next(e for e in events if isinstance(e, TurnEnded)).data.status == "done"
    assert held_by_taker
    assert all(held_by_taker)


async def test_an_unbound_inbound_source_fails_loudly(databases: Databases) -> None:
    """No lock or hub of its own: an unbound source raises instead of taking or redelivering."""
    source = SqliteInboundSource(databases)

    with pytest.raises(RuntimeError, match="not bound"):
        await source.take("prj_none", "ses_none", "trn_none")
    with pytest.raises(RuntimeError, match="not bound"):
        await source.turn_ended("prj_none", AGENT, "trn_none", "failed", None)


async def test_build_services_binds_the_inbound_source_before_anything_can_run_a_turn(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound to the app's session manager before the wake-ups, the kickoff and the scheduler
    start; once the app is up, its take runs (with nothing queued, it takes nothing)."""
    steps: list[str] = []
    bound: list[tuple[SqliteInboundSource, object]] = []

    bind = SqliteInboundSource.bind

    def bind_seen(self: SqliteInboundSource, sessions: Any) -> None:
        steps.append("bind")
        bound.append((self, sessions))
        bind(self, sessions)

    monkeypatch.setattr(SqliteInboundSource, "bind", bind_seen)
    for owner, name in ((WakeupService, "start"), (Kickoff, "resume"), (AppScheduler, "start")):
        original = getattr(owner, name)

        async def step_seen(
            self: Any, *a: Any, _o: Any = original, _n: str = name, **k: Any
        ) -> Any:
            steps.append(f"{type(self).__name__}.{_n}")
            return await _o(self, *a, **k)

        monkeypatch.setattr(owner, name, step_seen)

    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        project_id = await add_local_project(app, tmp_path, git_env)
        view = await services.sessions.get(project_id, "orchestrator")
        [(inbound, sessions)] = bound
        taken = await inbound.take(project_id, view.id, "trn_none")

    assert steps[0] == "bind"
    assert {"WakeupService.start", "Kickoff.resume", "AppScheduler.start"} <= set(steps)
    assert sessions is services.sessions
    assert taken == []
