"""A decision commits only while its turn waits (#381): Stop against a decision in both orders,
an approve followed at once by Stop, a decision while the turn runs, the Inbox list, a stall,
and a Stop cut between storing `stopped` and its sweep. Each race holds one side on purpose and
runs under a timeout, so a lost guard fails as a timeout of its own test."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.errors import DomainError
from tests.agents.repair_doubles import HeldObserver, cancelled_once
from tests.agents.repair_env import WRITER, RepairEnv
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.agents.runtime.test_resuming_turns import collect_until_turn_waiting
from tests.agents.turn_end_doubles import gate_then, hold, signalled

Interleaving = Literal["one after the other", "at once", "stop before the resumed worker runs"]


@pytest.mark.parametrize(
    "interleaving", ["one after the other", "at once", "stop before the resumed worker runs"]
)
async def test_an_approve_and_a_stop_at_once_end_the_turn_stopped(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch, interleaving: Interleaving
) -> None:
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)

    async def approve() -> None:
        await app.service.decide(repair.project_id, approval.id, approve=True, note=None)

    async def stop() -> None:
        await app.manager.stop(repair.project_id, WRITER)

    async with asyncio.timeout(15):
        if interleaving == "one after the other":
            await approve()
            await stop()
        elif interleaving == "at once":
            for result in await asyncio.gather(approve(), stop(), return_exceptions=True):
                # A decision that loses to the Stop is refused.
                assert result is None or (isinstance(result, DomainError) and result.status == 409)
        else:
            # The resumed worker is held before it takes the turn, and Stop has asked it to
            # stop that turn (its look for the waiting turn comes right after), so the turn
            # ends stopped without its executor running: the message is still `waiting` then.
            trigger = hold(monkeypatch, SessionStore, "turn_trigger_id", lambda *a: a[2] == turn_id)
            await approve()
            await trigger.wait_entered()
            looked = signalled(monkeypatch, SessionStore, "waiting_turn")
            stopping = asyncio.create_task(stop())
            await looked.wait()
            trigger.release.set()
            await stopping
            assert repair.write_tool.invoked == []
        await repair.turns.wait_ended(turn_id)

    assert repair.ends(turn_id) == ["stopped"]
    assert (await repair.message_of(approval)).status == "stopped"
    assert await app.store.pending(repair.project_id) == []
    assert len(await repair.audit_of(approval.id)) == 1
    assert len(await repair.events_of(approval.id)) == 1


async def test_a_stop_while_an_approve_is_in_its_observers_leaves_it_approved_and_unresumed(
    repair: RepairEnv,
) -> None:
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    held = HeldObserver([approval.id])
    app.store.add_observer(held)

    async with asyncio.timeout(15):
        deciding = asyncio.create_task(
            app.service.decide(repair.project_id, approval.id, approve=True, note=None)
        )
        await held.wait_entered(approval.id)
        await app.manager.stop(repair.project_id, WRITER)
        held.release[approval.id].set()
        await deciding
        await repair.turns.wait_ended(turn_id)

    stored = await repair.stored(approval.id)
    assert (stored.status, stored.decided_by) == ("approved", "user")
    assert (await repair.card(approval)).status == "approved"
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert repair.ends(turn_id) == ["stopped"]
    assert not await repair.running(app)
    assert repair.write_tool.invoked == []


async def test_a_decision_while_stop_sweeps_is_refused_and_the_sweep_cancels(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    turn_id, [approval] = await repair.ask(app=app)
    sweep = hold(monkeypatch, app.store, "cancel_for_turn")

    stopping = asyncio.create_task(app.manager.stop(repair.project_id, WRITER))
    try:
        async with asyncio.timeout(15):
            await sweep.wait_entered()  # `stopped` is stored
            with pytest.raises(DomainError) as refused:
                await app.service.decide(repair.project_id, approval.id, approve=True, note=None)
            assert (refused.value.code, refused.value.status) == ("approval_turn_not_waiting", 409)
            assert await repair.audit_of(approval.id) == []
            assert await repair.events_of(approval.id) == []
            sweep.release.set()
            await stopping
            await repair.turns.wait_ended(turn_id)
    finally:
        sweep.release.set()

    stored = await repair.stored(approval.id)
    assert (stored.status, stored.decided_by) == ("cancelled", "user")
    assert (await repair.card(approval)).status == "cancelled"
    assert await repair.audit_of(approval.id) == [("user", "cancelled")]
    assert await repair.events_of(approval.id) == [("user", "cancelled")]
    assert repair.ends(turn_id) == ["stopped"]


async def test_a_decision_while_its_turn_runs_is_refused_and_the_inbox_waits_for_the_turn(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    await app.manager.ensure_recovered(repair.project_id)
    # A pending approval of a turn that ended, which nothing swept (recovery ran before it).
    left = await repair.seed("trn_done", turn_status="done")
    gate = gate_then(monkeypatch, "hold")
    stream = await repair.stream(app)
    repair.script_writes()
    await app.manager.send(repair.project_id, WRITER, "Please write")
    try:
        async with asyncio.timeout(15):
            await gate.wait_entered()
            [asked] = [view for view in app.observer.views if view.status == "pending"]
            pending = {a.id for a in await app.store.pending(repair.project_id)}
            assert pending == {left.id, asked.id}
            assert await app.service.pending(repair.project_id) == []
            with pytest.raises(DomainError) as refused:
                await app.service.decide(repair.project_id, asked.id, approve=True, note=None)
            assert (refused.value.code, refused.value.status) == ("approval_turn_not_waiting", 409)
            assert (await repair.stored(asked.id)).status == "pending"
            assert await repair.audit_of(asked.id) == []
            assert await repair.events_of(asked.id) == []

            gate.release.set()
            await collect_until_turn_waiting(stream)
            assert [v.id for v in await app.service.pending(repair.project_id)] == [asked.id]
            await app.service.decide(repair.project_id, asked.id, approve=True, note=None)
            await repair.turns.wait_ended(asked.turn_id)
    finally:
        gate.release.set()

    assert repair.ends(asked.turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 1


@pytest.mark.parametrize(("approve", "end"), [(True, "done"), (False, "stopped")])
async def test_a_waiting_stall_is_decided_under_the_guard(
    repair: RepairEnv, approve: bool, end: str
) -> None:
    stall = await repair.ask_stall()
    stream = await repair.stream(repair.first, "orchestrator")

    await repair.first.service.decide(repair.project_id, stall.id, approve=approve, note=None)

    found = await collect_until_turn_ended(stream, timeout=10)
    ended = next(e for e in reversed(found) if isinstance(e, TurnEnded))
    assert (ended.data.turn_id, ended.data.status) == (stall.turn_id, end)
    assert (await repair.stored(stall.id)).status == ("approved" if approve else "rejected")
    assert await repair.stall_resumed(stall.turn_id) == (1 if approve else 0)


async def test_a_stop_cut_between_stopped_and_its_sweep_leaves_the_approvals_to_recovery(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    turn_id, approvals = await repair.ask(calls=2, app=app)
    monkeypatch.setattr(app.store, "cancel_for_turn", cancelled_once(app.store.cancel_for_turn))
    with pytest.raises(asyncio.CancelledError):
        await app.manager.stop(repair.project_id, WRITER)
    assert await repair.turn_status(turn_id) == "stopped"

    restarted = repair.restart()
    await restarted.manager.ensure_recovered(repair.project_id)

    for approval in approvals:
        stored = await repair.stored(approval.id)
        assert (stored.status, stored.decided_by) == ("expired", "system")
        assert (await repair.card(approval)).status == "expired"
        assert await repair.audit_of(approval.id) == [("system", "expired")]
        assert await repair.events_of(approval.id) == [("system", "expired")]
    assert await repair.turn_status(turn_id) == "stopped"
    assert (await repair.message_of(approvals[0])).status == "stopped"
