"""The restart repair against everything else that can touch the same approvals: a second
repair, a live decision, a close, and a Stop sent first after the restart. Each test holds the
other side on purpose, so what it asserts holds in every interleaving."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mlops_agents.agents.runtime.repair import repair_unsettled_approvals
from tests.agents.repair_env import WRITER, HeldObserver, RepairEnv


async def test_two_managers_one_after_the_other_repair_once(repair: RepairEnv) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    second = repair.restart()
    stream = await repair.stream(second)
    await second.manager.ensure_recovered(repair.project_id)
    assert await repair.ended(stream) == (turn_id, "done")

    third = repair.restart()
    await third.manager.ensure_recovered(repair.project_id)

    assert third.observer.calls == []
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 1


async def test_two_first_uses_at_once_repair_once(repair: RepairEnv) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()
    stream = await repair.stream(app)

    await asyncio.gather(
        app.manager.ensure_recovered(repair.project_id),
        app.manager.ensure_recovered(repair.project_id),
    )

    assert await repair.ended(stream) == (turn_id, "done")
    assert app.observer.calls == [("decided", approval.id, "approved")]
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 1


async def test_a_repair_racing_a_live_decision_writes_and_resumes_once(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_id, [approval] = await repair.ask()
    app = repair.restart()
    stream = await repair.stream(app)
    await app.manager.ensure_recovered(repair.project_id)
    rewrite = app.manager.update_message
    entered, release = asyncio.Event(), asyncio.Event()
    holds = 0

    async def held_rewrite(*args: Any, **kwargs: Any) -> Any:
        nonlocal holds
        holds += 1
        if holds == 1:  # the live decide's card, its records written
            entered.set()
            await release.wait()
        return await rewrite(*args, **kwargs)

    monkeypatch.setattr(app.manager, "update_message", held_rewrite)
    deciding = asyncio.create_task(
        app.service.decide(repair.project_id, approval.id, approve=True, note=None)
    )
    await asyncio.wait_for(entered.wait(), timeout=10)

    await repair_unsettled_approvals(
        repair.project_id,
        unsettled=[await repair.stored(approval.id)],
        approval_store=app.store,
        store=repair.sessions,
        databases=repair.databases,
        audit_log=repair.stall.audit,
        event_log=repair.stall.events,
        rewrite_part=app.manager.update_message,
        stop_turn=app.manager.stop_turn,
        resume_turn=app.manager.resume_turn,
    )
    release.set()
    await deciding

    assert await repair.ended(stream) == (turn_id, "done")
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert (await repair.card(approval)).status == "approved"
    assert (await repair.stored(approval.id)).settled_at is not None
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 1


async def test_a_live_decision_in_its_observers_is_neither_retold_nor_resumed_by_the_repair(
    repair: RepairEnv,
) -> None:
    turn_id, [cut, live] = await repair.ask(calls=2)
    await repair.crash_deciding(cut.id)
    app = repair.restart()
    held = HeldObserver([cut.id, live.id])
    app.store.add_observer(held)
    stream = await repair.stream(app)

    first_use = asyncio.create_task(app.manager.ensure_recovered(repair.project_id))
    await held.wait_entered(cut.id)  # the repair re-tells the cut decision; the project counts
    # as recovered, so this decision passes the recovery at once and commits
    deciding = asyncio.create_task(
        app.service.decide(repair.project_id, live.id, approve=True, note=None)
    )
    await held.wait_entered(live.id)
    held.release[cut.id].set()
    await first_use  # the whole repair, while the live decision is still in its observers

    assert await repair.turn_status(turn_id) == "waiting"
    assert not await repair.running(app)
    assert [c for c in app.observer.calls if c[1] == live.id] == [("decided", live.id, "approved")]

    held.release[live.id].set()
    await deciding

    assert await repair.ended(stream) == (turn_id, "done")
    assert held.finished == [cut.id, live.id]
    assert [c for c in app.observer.calls if c[1] == live.id] == [("decided", live.id, "approved")]
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 2


async def test_close_time_recovery_repairs_nothing(repair: RepairEnv) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()

    await app.manager.close_project(repair.project_id)

    assert app.observer.calls == []
    assert (await repair.stored(approval.id)).settled_at is None
    assert (await repair.card(approval)).status == "pending"
    assert await repair.audit_of(approval.id) == []
    assert await repair.turn_status(turn_id) == "waiting"
    assert (repair.ends(turn_id), repair.waits(turn_id)) == ([], 0)


async def test_a_stop_sent_first_after_the_restart_repairs_then_stops(repair: RepairEnv) -> None:
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)
    app = repair.restart()
    stream = await repair.stream(app)

    await app.manager.stop(repair.project_id, WRITER)

    # Only what holds wherever the Stop lands in the resumed worker: the agent message's status
    # depends on whether the worker had started the turn's task yet.
    assert await repair.ended(stream) == (turn_id, "stopped")
    assert await app.store.pending(repair.project_id) == []
    assert (await repair.card(approval)).status == "approved"
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
