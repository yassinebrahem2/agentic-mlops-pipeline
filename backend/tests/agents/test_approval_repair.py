"""The restart repair (#381): a decision a crash cut after its status commit is told to the
observers again, its records and card are written once, and its waiting turn is finished, the
first time the project is used after the restart."""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import delete, update

from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.runtime.contract import ToolResultPart
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.tables import approvals, messages
from tests.agents.repair_env import (
    CancelledOnceAuditLog,
    Decision,
    RepairEnv,
    cancelled_once,
)
from tests.agents.settle_env import CancelledOnceEventLog


@pytest.mark.parametrize(
    ("decision", "status", "actor", "note"),
    [
        ("approve", "approved", "user", "Go ahead"),
        ("reject", "rejected", "user", "Too risky"),
        ("cancel", "cancelled", "system", None),
    ],
)
async def test_a_decision_cut_after_its_status_commit_is_finished_at_the_first_use(
    repair: RepairEnv, decision: Decision, status: str, actor: str, note: str | None
) -> None:
    turn_id, [approval] = await repair.ask()

    await repair.crash_deciding(approval.id, decision, note)

    cut = await repair.stored(approval.id)
    assert (cut.status, cut.decided_by, cut.settled_at) == (status, actor, None)
    assert (await repair.card(approval)).status == "pending"
    assert await repair.turn_status(turn_id) == "waiting"
    assert (await repair.audit_of(approval.id), await repair.events_of(approval.id)) == ([], [])

    app = repair.restart()
    stream = await repair.stream(app)
    await app.manager.ensure_recovered(repair.project_id)

    assert await repair.ended(stream) == (turn_id, "done")
    card = await repair.card(approval)
    assert (card.status, card.note) == (status, note)
    assert (await repair.stored(approval.id)).settled_at is not None
    assert await repair.audit_of(approval.id) == [(actor, status)]
    assert await repair.events_of(approval.id) == [(actor, status)]
    assert app.observer.calls == [("decided", approval.id, status)]
    assert repair.ends(turn_id) == ["done"]
    message = await repair.message_of(approval)
    [result] = [p for p in message.parts if isinstance(p, ToolResultPart)]
    if decision == "approve":
        assert (len(repair.write_tool.invoked), result.is_error) == (1, False)
    else:
        assert (len(repair.write_tool.invoked), result.is_error) == (0, True)
        assert "The user rejected this action" in result.content
        assert note is None or note in result.content


async def test_a_turn_with_an_approval_still_pending_is_settled_but_not_resumed(
    repair: RepairEnv,
) -> None:
    turn_id, [first, second] = await repair.ask(calls=2)
    await repair.crash_deciding(first.id)

    app = repair.restart()
    stream = await repair.stream(app)
    await app.manager.ensure_recovered(repair.project_id)

    assert ((await repair.card(first)).status, (await repair.card(second)).status) == (
        "approved",
        "pending",
    )
    assert (await repair.stored(first.id)).settled_at is not None
    assert await repair.audit_of(first.id) == [("user", "approved")]
    assert await repair.turn_status(turn_id) == "waiting"
    assert not await repair.running(app)
    assert (repair.ends(turn_id), repair.waits(turn_id)) == ([], 0)

    await app.service.decide(repair.project_id, second.id, approve=True, note=None)

    assert await repair.ended(stream) == (turn_id, "done")
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 2


WINDOWS = ["before the audit record", "before the event", "before the card", "before settled_at"]


def _dying_service(
    repair: RepairEnv, window: str, monkeypatch: pytest.MonkeyPatch
) -> ApprovalsService:
    """An approvals service of the first app whose settle dies at `window`."""
    first = repair.first
    audit_log: AuditLog = repair.stall.audit
    event_log: EventLog = repair.stall.events
    if window == "before the audit record":
        audit_log = CancelledOnceAuditLog(repair.databases)
    elif window == "before the event":
        event_log = CancelledOnceEventLog(repair.databases)
    elif window == "before the card":
        rewrite = cancelled_once(first.manager.update_message)
        monkeypatch.setattr(first.manager, "update_message", rewrite)
    else:
        monkeypatch.setattr(first.store, "mark_settled", cancelled_once(first.store.mark_settled))
    return ApprovalsService(
        repair.databases, first.manager, store=first.store, audit_log=audit_log, event_log=event_log
    )


@pytest.mark.parametrize("window", WINDOWS)
async def test_a_settle_cut_anywhere_is_finished_once_at_restart(
    repair: RepairEnv, window: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_id, [approval] = await repair.ask()
    service = _dying_service(repair, window, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await service.decide(repair.project_id, approval.id, approve=True, note=None)
    assert (await repair.stored(approval.id)).settled_at is None

    app = repair.restart()
    stream = await repair.stream(app)
    await app.manager.ensure_recovered(repair.project_id)

    assert await repair.ended(stream) == (turn_id, "done")
    assert await repair.audit_of(approval.id) == [("user", "approved")]
    assert await repair.events_of(approval.id) == [("user", "approved")]
    assert (await repair.card(approval)).status == "approved"
    assert (await repair.stored(approval.id)).settled_at is not None
    assert repair.ends(turn_id) == ["done"]
    assert len(repair.write_tool.invoked) == 1


@pytest.mark.parametrize(
    ("approve", "status", "end", "resumed"),
    [(True, "approved", "done", 1), (False, "rejected", "stopped", 0)],
)
async def test_a_stall_decision_cut_at_its_commit_resumes_or_stops_its_turn(
    repair: RepairEnv, approve: bool, status: str, end: str, resumed: int
) -> None:
    stall = await repair.ask_stall()
    await repair.crash_deciding(stall.id, "approve" if approve else "reject")

    app = repair.restart()
    stream = await repair.stream(app, "orchestrator")
    await app.manager.ensure_recovered(repair.project_id)

    assert await repair.ended(stream) == (stall.turn_id, end)
    assert (await repair.card(stall)).status == status
    assert (await repair.stored(stall.id)).settled_at is not None
    assert await repair.events_of(stall.id) == [("user", status)]
    assert repair.ends(stall.turn_id) == [end]
    assert await repair.stall_resumed(stall.turn_id) == resumed


async def test_a_stop_cut_after_its_sweep_gets_its_cards_and_records_at_restart(
    repair: RepairEnv,
) -> None:
    # What a Stop leaves when it dies right after its sweep commits: the turn is stored stopped
    # first (task 3's order), its approvals cancelled by the user, nothing else written.
    approval = await repair.seed("trn_stopped", turn_status="stopped")
    await repair.first.store.cancel_for_turn(repair.project_id, "trn_stopped", decided_by="user")

    app = repair.restart()
    await app.manager.ensure_recovered(repair.project_id)

    assert (await repair.card(approval)).status == "cancelled"
    assert await repair.audit_of(approval.id) == [("user", "cancelled")]
    assert await repair.events_of(approval.id) == [("user", "cancelled")]
    assert (await repair.stored(approval.id)).settled_at is not None
    assert app.observer.calls == [("decided", approval.id, "cancelled")]
    assert await repair.turn_status("trn_stopped") == "stopped"


async def test_an_approval_settled_by_the_migration_is_not_touched(repair: RepairEnv) -> None:
    # A row decided before 0006: its decider is unknown, and it counts as settled.
    approval = await repair.seed("trn_old", turn_status="done")
    engine = await repair.databases.project(repair.project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(approvals)
            .where(approvals.c.id == approval.id)
            .values(
                status="approved", decided_at=approval.created_at, settled_at=approval.created_at
            )
        )

    app = repair.restart()
    await app.manager.ensure_recovered(repair.project_id)

    assert app.observer.calls == []
    assert (await repair.audit_of(approval.id), await repair.events_of(approval.id)) == ([], [])
    assert (await repair.card(approval)).status == "pending"
    assert (await repair.stored(approval.id)).decided_by is None


async def test_an_approval_whose_message_is_gone_is_settled_with_the_rest_of_its_batch(
    repair: RepairEnv, caplog: pytest.LogCaptureFixture
) -> None:
    gone = await repair.seed("trn_gone", turn_status="stopped")
    await repair.first.store.cancel_for_turn(repair.project_id, "trn_gone", decided_by="user")
    engine = await repair.databases.project(repair.project_id)
    async with engine.begin() as conn:
        await conn.execute(delete(messages).where(messages.c.id == gone.message_id))
    turn_id, [approval] = await repair.ask()
    await repair.crash_deciding(approval.id)

    app = repair.restart()
    stream = await repair.stream(app)
    with caplog.at_level(logging.WARNING):
        await app.manager.ensure_recovered(repair.project_id)

    assert await repair.ended(stream) == (turn_id, "done")
    assert (await repair.stored(gone.id)).settled_at is not None
    assert await repair.audit_of(gone.id) == [("user", "cancelled")]
    assert (await repair.stored(approval.id)).settled_at is not None
    assert (await repair.card(approval)).status == "approved"
    assert any(gone.message_id in r.getMessage() for r in caplog.records)
