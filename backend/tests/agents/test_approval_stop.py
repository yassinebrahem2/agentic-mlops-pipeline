"""Every turn end cancels the approvals its turn left pending (#381): Stop of a running or a
waiting turn, a turn that ends done or failed, and one whose wait could not be stored; an
interrupted turn's approvals are left to recovery. Each test waits until the turn observers
heard the end, which comes after the sweep."""

from __future__ import annotations

from typing import Literal

import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.runtime.contract import ApprovalPart, TurnWaiting
from mlops_agents.agents.runtime.store import SessionStore
from tests.agents.repair_env import WRITER, App, RepairEnv
from tests.agents.runtime.test_resuming_turns import collect_until_turn_waiting
from tests.agents.turn_end_doubles import Hold, gate_then, hold


async def _asked(repair: RepairEnv, app: App, gate: Hold) -> ApprovalView:
    """Send to the writer; the approval its gate asked for, once asked."""
    repair.script_writes()
    await app.manager.send(repair.project_id, WRITER, "Please write")
    await gate.wait_entered()
    [asked] = [view for view in app.observer.views if view.status == "pending"]
    return asked


async def test_a_stop_before_the_turn_waits_cancels_its_approval_and_invents_no_card(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    asked = await _asked(repair, app, gate_then(monkeypatch, "hold"))

    await app.manager.stop(repair.project_id, WRITER)
    await repair.turns.wait_ended(asked.turn_id)

    stored = await repair.stored(asked.id)
    assert (stored.status, stored.decided_by) == ("cancelled", "user")
    assert stored.settled_at is not None
    assert app.observer.calls == [
        ("asked", asked.id, "pending"),
        ("decided", asked.id, "cancelled"),
    ]
    assert await repair.audit_of(asked.id) == [("user", "cancelled")]
    assert await repair.events_of(asked.id) == [("user", "cancelled")]
    assert repair.ends(asked.turn_id) == ["stopped"]
    message = await repair.message_of(stored)
    assert message.status == "stopped"
    assert not [part for part in message.parts if isinstance(part, ApprovalPart)]


async def test_a_stop_once_the_card_is_written_shows_it_cancelled(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    # The card and the waiting message are stored; the turn is not marked waiting yet.
    wait = hold(monkeypatch, SessionStore, "set_turn", lambda *args, **_: args[3] == "waiting")
    repair.script_writes()
    await app.manager.send(repair.project_id, WRITER, "Please write")
    await wait.wait_entered()
    [approval] = await app.store.pending(repair.project_id)
    assert (await repair.card(approval)).status == "pending"

    await app.manager.stop(repair.project_id, WRITER)
    await repair.turns.wait_ended(approval.turn_id)

    assert (await repair.stored(approval.id)).status == "cancelled"
    assert (await repair.card(approval)).status == "cancelled"
    assert await repair.audit_of(approval.id) == [("user", "cancelled")]
    assert await repair.events_of(approval.id) == [("user", "cancelled")]
    assert repair.ends(approval.turn_id) == ["stopped"]


@pytest.mark.parametrize(("then", "end"), [("allow", "done"), ("fail", "failed")])
async def test_a_turn_that_ends_with_an_approval_pending_has_the_system_cancel_it(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch, then: Literal["allow", "fail"], end: str
) -> None:
    app = repair.restart()
    asked = await _asked(repair, app, gate_then(monkeypatch, then))

    await repair.turns.wait_ended(asked.turn_id)

    assert repair.ends(asked.turn_id) == [end]
    stored = await repair.stored(asked.id)
    assert (stored.status, stored.decided_by) == ("cancelled", "system")
    assert stored.settled_at is not None
    assert app.observer.calls == [
        ("asked", asked.id, "pending"),
        ("decided", asked.id, "cancelled"),
    ]
    assert await repair.audit_of(asked.id) == [("system", "cancelled")]
    assert await repair.events_of(asked.id) == [("system", "cancelled")]


async def test_an_interrupted_turn_leaves_its_approval_to_recovery(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    asked = await _asked(repair, app, gate_then(monkeypatch, "hold"))

    await app.manager.close_project(repair.project_id)

    assert repair.ends(asked.turn_id) == ["interrupted"]
    stored = await repair.stored(asked.id)
    assert (stored.status, stored.decided_by) == ("expired", "system")
    assert await repair.audit_of(asked.id) == [("system", "expired")]
    assert await repair.events_of(asked.id) == [("system", "expired")]


async def test_a_turn_whose_wait_cannot_be_stored_fails_and_cancels_its_card(
    repair: RepairEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = repair.restart()
    set_turn = SessionStore.set_turn

    async def no_wait(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "waiting":
            raise RuntimeError("disk full")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", no_wait)
    stream = await repair.stream(app)
    repair.script_writes()
    await app.manager.send(repair.project_id, WRITER, "Please write")
    turn_id, status = await repair.ended(stream)

    assert status == "failed"
    [approval] = await app.store.list_for_step(repair.project_id, turn_id)
    assert (approval.status, approval.decided_by) == ("cancelled", "system")
    assert (await repair.card(approval)).status == "cancelled"
    assert (await repair.message_of(approval)).status == "complete"
    assert await repair.audit_of(approval.id) == [("system", "cancelled")]
    assert await repair.events_of(approval.id) == [("system", "cancelled")]


async def test_a_stop_of_a_waiting_turn_cancels_every_card_and_starts_the_next_turn(
    repair: RepairEnv,
) -> None:
    app = repair.restart()
    turn_id, approvals = await repair.ask(calls=2, app=app)
    stream = await repair.stream(app)
    await app.manager.send(repair.project_id, WRITER, "And then this")

    await app.manager.stop(repair.project_id, WRITER)
    await repair.turns.wait_ended(turn_id)

    for approval in approvals:
        stored = await repair.stored(approval.id)
        assert (stored.status, stored.decided_by) == ("cancelled", "user")
        assert (await repair.card(approval)).status == "cancelled"
        assert await repair.audit_of(approval.id) == [("user", "cancelled")]
        assert await repair.events_of(approval.id) == [("user", "cancelled")]
    assert (await repair.message_of(approvals[0])).status == "stopped"
    assert repair.ends(turn_id) == ["stopped"]
    assert await repair.stall.checkpoint(turn_id) is None
    # The queued turn runs: its step asks again, and it waits.
    waited = await collect_until_turn_waiting(stream)
    [next_turn] = [e.data.turn_id for e in waited if isinstance(e, TurnWaiting)]
    assert next_turn != turn_id
