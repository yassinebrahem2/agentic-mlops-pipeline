"""A Stop is never lost and never hits the wrong turn (#446): a Stop against an approve's resume
in each order (windows A and C), the Send that queues behind it or behind a lost take, a Stop
while a turn's wait is reported, two quick Stops, and `stop_turn` beside another turn. Each race
holds one side on purpose, at a point both the old and the fixed code reach, under a timeout."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from mlops_agents.agents.approvals.store import ApprovalRecord
from mlops_agents.agents.runtime.hub import SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from tests.agents.repair_env import WRITER, App, RepairEnv
from tests.agents.runtime.stop_doubles import (
    CancelRecordingHold,
    LockWaits,
    first_of,
    stop_returned,
)
from tests.agents.turn_end_doubles import hold, signalled

Stopper = Literal["stop", "stop_turn"]


async def _stop(app: App, repair: RepairEnv, approval: ApprovalRecord, stopper: Stopper) -> None:
    if stopper == "stop":
        await app.manager.stop(repair.project_id, WRITER)
    else:
        await app.manager.stop_turn(repair.project_id, approval.session_id, approval.turn_id)


async def _approve(app: App, repair: RepairEnv, approval: ApprovalRecord) -> None:
    await app.service.decide(repair.project_id, approval.id, approve=True, note=None)


async def _until_another_turn_ended(repair: RepairEnv, turn_id: str) -> None:
    """Until a turn other than `turn_id` has ended, or 5 s have passed (the caller asserts)."""
    try:
        async with asyncio.timeout(5):
            while True:
                if [t for t, _ in repair.turns.ended if t != turn_id]:
                    break
                await asyncio.sleep(0.02)
    except TimeoutError:
        return


@pytest.mark.parametrize("stopper", ["stop", "stop_turn"])
async def test_a_stop_whose_look_comes_after_the_resumed_worker_took_the_turn_stops_it(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch, stopper: Stopper
) -> None:
    """Window A, Stop deciding after the resume starts its worker."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    requests = len(repair.fake.state.requests)
    resuming = hold(monkeypatch, SessionStore, "turn_status")
    look = hold(monkeypatch, SessionStore, "waiting_turn")
    taken = signalled(monkeypatch, SessionStore, "take_waiting_turn")
    classified = stop_returned(monkeypatch)

    try:
        async with asyncio.timeout(15):
            deciding = asyncio.create_task(_approve(app, repair, approval))
            await resuming.wait_entered()  # the resume holds the session lock
            stopping = asyncio.create_task(_stop(app, repair, approval, stopper))
            await first_of(classified, look.entered)  # Stop found no worker yet
            resuming.release.set()
            await taken.wait()
            look.release.set()
            await deciding
            await stopping
            await repair.turns.wait_ended(turn_id)
    finally:
        resuming.release.set()
        look.release.set()

    assert repair.ends(turn_id) == ["stopped"]
    assert len(repair.fake.state.requests) == requests
    assert repair.write_tool.invoked == []


@pytest.mark.parametrize("stopper", ["stop", "stop_turn"])
async def test_a_stop_decided_before_the_resume_starts_its_worker_stops_the_turn(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch, stopper: Stopper
) -> None:
    """Window A, Stop deciding before the resume takes the session lock."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    requests = len(repair.fake.state.requests)
    resume_starting = hold(monkeypatch, SessionStore, "session_agent")

    try:
        async with asyncio.timeout(15):
            deciding = asyncio.create_task(_approve(app, repair, approval))
            await resume_starting.wait_entered()
            await _stop(app, repair, approval, stopper)
            resume_starting.release.set()
            await deciding
            await repair.turns.wait_ended(turn_id)
    finally:
        resume_starting.release.set()

    assert repair.ends(turn_id) == ["stopped"]
    assert await repair.turn_status(turn_id) == "stopped"
    assert len(repair.fake.state.requests) == requests
    assert repair.write_tool.invoked == []


@pytest.mark.parametrize("stopper", ["stop", "stop_turn"])
async def test_a_stop_that_finds_the_resumed_worker_before_its_take_stops_the_turn(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch, stopper: Stopper
) -> None:
    """Window C: the resume's worker exists and has not taken the turn when Stop looks."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    requests = len(repair.fake.state.requests)
    resuming = hold(monkeypatch, SessionStore, "turn_status")
    trigger = hold(monkeypatch, SessionStore, "turn_trigger_id", lambda *a: a[2] == turn_id)
    look = hold(monkeypatch, SessionStore, "waiting_turn")
    looked = signalled(monkeypatch, SessionStore, "waiting_turn")
    classified = stop_returned(monkeypatch)

    try:
        async with asyncio.timeout(15):
            deciding = asyncio.create_task(_approve(app, repair, approval))
            await resuming.wait_entered()
            stopping = asyncio.create_task(_stop(app, repair, approval, stopper))
            await first_of(classified, look.entered)
            resuming.release.set()
            await trigger.wait_entered()  # the worker exists, before its take
            look.release.set()
            stopped = asyncio.Event()
            stopping.add_done_callback(lambda _: stopped.set())
            await first_of(looked, stopped)
            trigger.release.set()
            await deciding
            await stopping
            await repair.turns.wait_ended(turn_id)
    finally:
        resuming.release.set()
        trigger.release.set()
        look.release.set()

    assert repair.ends(turn_id) == ["stopped"]
    assert len(repair.fake.state.requests) == requests
    assert repair.write_tool.invoked == []


async def test_a_message_sent_right_after_a_stop_of_a_waiting_turn_runs(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Send queues on the session lock behind the Stop, which comes as the resume starts."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    repair.fake.state.scripted_tool_calls = []  # the next turns answer and end
    resuming = hold(monkeypatch, SessionStore, "turn_status")
    locks = LockWaits(monkeypatch)

    try:
        async with asyncio.timeout(20):
            deciding = asyncio.create_task(_approve(app, repair, approval))
            await resuming.wait_entered()
            stopping = asyncio.create_task(app.manager.stop(repair.project_id, WRITER))
            await locks.watch(stopping).wait()
            sending = asyncio.create_task(app.manager.send(repair.project_id, WRITER, "Next"))
            await locks.watch(sending).wait()
            resuming.release.set()
            await deciding
            await stopping
            sent = await sending
            await repair.turns.wait_ended(turn_id)
            await _until_another_turn_ended(repair, turn_id)
    finally:
        resuming.release.set()

    message = await repair.sessions.message(repair.project_id, approval.session_id, sent.id)
    assert message is not None
    assert message.status == "complete"
    assert repair.ends(turn_id) == ["stopped"]
    assert [s for t, s in repair.turns.ended if t != turn_id] == ["done"]


async def test_a_message_queued_behind_a_resume_whose_take_lost_runs(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resumed turn is stored stopped by another writer than Stop (approval cancellation or
    the restart repair) before the resume's worker takes it; a Send queued meanwhile still runs."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    repair.fake.state.scripted_tool_calls = []  # the next turns answer and end
    resuming = hold(monkeypatch, SessionStore, "turn_status")
    trigger = hold(monkeypatch, SessionStore, "turn_trigger_id", lambda *a: a[2] == turn_id)
    locks = LockWaits(monkeypatch)

    try:
        async with asyncio.timeout(20):
            deciding = asyncio.create_task(_approve(app, repair, approval))
            await resuming.wait_entered()
            sending = asyncio.create_task(app.manager.send(repair.project_id, WRITER, "Next"))
            await locks.watch(sending).wait()
            resuming.release.set()
            await deciding
            sent = await sending
            assert sent.status == "queued"
            await trigger.wait_entered()
            await repair.sessions.set_turn(repair.project_id, turn_id, "stopped")
            trigger.release.set()
            await _until_another_turn_ended(repair, turn_id)
    finally:
        resuming.release.set()
        trigger.release.set()

    message = await repair.sessions.message(repair.project_id, approval.session_id, sent.id)
    assert message is not None
    assert message.status == "complete"
    assert await repair.turn_status(turn_id) == "stopped"
    assert [s for t, s in repair.turns.ended if t != turn_id] == ["done"]


async def test_a_stop_while_the_wait_is_reported_stops_the_turn_once_reported(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    reporting = CancelRecordingHold(monkeypatch, SessionHub, "turn_waiting")
    classified = stop_returned(monkeypatch)
    repair.script_writes()

    try:
        async with asyncio.timeout(15):
            await app.manager.send(repair.project_id, WRITER, "Please write")
            await asyncio.wait_for(reporting.entered.wait(), timeout=10)
            [approval] = await app.store.pending(repair.project_id)
            stopping = asyncio.create_task(app.manager.stop(repair.project_id, WRITER))
            await classified.wait()
            reporting.release.set()
            await stopping
            await repair.turns.wait_ended(approval.turn_id)
    finally:
        reporting.release.set()

    assert not reporting.cancelled
    assert repair.waits(approval.turn_id) == 1
    assert repair.ends(approval.turn_id) == ["stopped"]
    assert await repair.turn_status(approval.turn_id) == "stopped"
    assert (await repair.stored(approval.id)).status == "cancelled"


async def test_a_second_stop_before_the_next_worker_takes_its_turn_leaves_that_turn_running(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    repair.fake.state.scripted_tool_calls = []  # the next turns answer and end
    await app.manager.send(repair.project_id, WRITER, "Next, please")
    queued = await repair.sessions.next_queued_turn(repair.project_id, approval.session_id)
    assert queued is not None
    requests = len(repair.fake.state.requests)
    # The first look for a queued turn is the first Stop's (it starts a worker for that turn);
    # the second, under the lock, is that worker's first look.
    looks = 0

    def worker_first_look(*_: object) -> bool:
        nonlocal looks
        looks += 1
        return looks == 2

    first_look = hold(monkeypatch, SessionStore, "next_queued_turn", worker_first_look)

    try:
        async with asyncio.timeout(15):
            await app.manager.stop(repair.project_id, WRITER)
            await first_look.wait_entered()
            await app.manager.stop(repair.project_id, WRITER)
            first_look.release.set()
            await repair.turns.wait_ended(queued[0])
    finally:
        first_look.release.set()

    assert repair.ends(turn_id) == ["stopped"]
    assert repair.ends(queued[0]) == ["done"]
    assert len(repair.fake.state.requests) > requests


async def test_stop_turn_of_a_waiting_turn_leaves_the_turn_the_worker_runs(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting turn beside a worker on another turn, built directly: the busy rule keeps a
    session from reaching it."""
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    repair.fake.state.scripted_tool_calls = []  # the next turns answer and end
    await app.manager.send(repair.project_id, WRITER, "Next, please")
    queued = await repair.sessions.next_queued_turn(repair.project_id, approval.session_id)
    assert queued is not None
    other = queued[0]
    running = hold(
        monkeypatch, SessionStore, "set_turn", lambda *a, **_: a[2] == other and a[3] == "running"
    )
    looked = signalled(monkeypatch, SessionStore, "waiting_turn")
    classified = stop_returned(monkeypatch)

    try:
        async with asyncio.timeout(15):
            await app.manager.stop(repair.project_id, WRITER)  # the worker takes `other`
            await running.wait_entered()
            classified.clear()
            looked.clear()
            await repair.sessions.set_turn(repair.project_id, turn_id, "waiting")
            requests = len(repair.fake.state.requests)
            stopping = asyncio.create_task(
                app.manager.stop_turn(repair.project_id, approval.session_id, turn_id)
            )
            await first_of(classified, looked)
            running.release.set()
            await stopping
            await repair.turns.wait_ended(other)
    finally:
        running.release.set()

    assert repair.ends(other) == ["done"]
    assert len(repair.fake.state.requests) > requests
