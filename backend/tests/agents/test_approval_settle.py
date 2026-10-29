"""One owner of a decided approval's records and card (`settle_decisions`): each record is
written once, the records before the card, `settled_at` last; and who decided an approval is
stored with the decision (#381)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import event, func, select

from mlops_agents.agents.approvals import store as store_module
from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.runtime.contract import Part
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit
from tests.agents.approval_env import STALL_PREVIEW, add_turn
from tests.agents.settle_env import (
    PROJECT,
    CancelledOnceEventLog,
    SettleEnv,
    SlowAuditLog,
    SlowEventLog,
    cancelled_rewrite,
)
from tests.agents.test_approvals_service import FakeSessionsForApproval

# record_decisions: once per approval


async def test_record_decisions_twice_writes_one_audit_record_and_one_event(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)

    await env.record(apr, event_log=env.event_log)
    await env.record(apr, event_log=env.event_log)

    assert await env.audit_actors(apr.id) == ["user"]
    assert await env.event_sources(apr.id) == ["user"]


async def test_with_the_audit_record_written_and_no_event_only_the_event_is_written(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    await env.record(apr, event_log=None)
    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], [])

    await env.record(apr, event_log=env.event_log)

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], ["user"])


async def test_a_stall_writes_its_event_once_and_no_audit_record(databases: Databases) -> None:
    env = SettleEnv(databases)
    await add_turn(databases, PROJECT)
    stall, _ = await env.store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)

    await env.record(stall, event_log=env.event_log)
    await env.record(stall, event_log=env.event_log)

    assert await env.event_sources(stall.id) == ["user"]
    engine = await databases.project(PROJECT)
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(audit))).scalar_one() == 0


async def test_the_audit_and_event_checks_use_their_indexes(databases: Databases) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    engine = await databases.project(PROJECT)
    checks: list[tuple[str, Any]] = []

    def capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT") and (
            "FROM audit" in statement or "FROM events" in statement
        ):
            checks.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await env.record(apr, event_log=env.event_log)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(checks) == 2
    async with engine.connect() as conn:
        for statement, parameters in checks:
            rows = await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
            plan = "\n".join(str(detail) for *_, detail in rows)
            index = "audit_approval_id" if "FROM audit" in statement else "events_type_subject"
            assert index in plan, plan
            assert "SCAN" not in plan, plan


# settle_decisions: records, then the card, then settled_at


async def test_a_cancelled_card_write_leaves_the_records_and_no_settled_at(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    decided = await env.store.decide(PROJECT, apr.id, approve=True, note="fine")

    with pytest.raises(asyncio.CancelledError):
        await env.settle([decided], rewrite_part=cancelled_rewrite)

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], ["user"])
    assert (await env.card(apr)).status == "pending"
    assert (await env.stored(apr.id)).settled_at is None

    await env.settle([decided])

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], ["user"])
    card = await env.card(apr)
    assert (card.status, card.note) == ("approved", "fine")
    assert (await env.stored(apr.id)).settled_at is not None


async def test_a_cancelled_record_write_leaves_no_card_and_no_settled_at(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    decided = await env.store.decide(PROJECT, apr.id, approve=False, note="no")
    log = CancelledOnceEventLog(databases)

    with pytest.raises(asyncio.CancelledError):
        await env.settle([decided], event_log=log)

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], [])
    assert (await env.card(apr)).status == "pending"
    assert (await env.stored(apr.id)).settled_at is None

    await env.settle([decided], event_log=log)

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], ["user"])
    assert ((await env.card(apr)).status, (await env.stored(apr.id)).settled_at is None) == (
        "rejected",
        False,
    )


async def test_settled_at_is_written_once(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = (f"2026-10-29T10:00:{n:02d}.000Z" for n in itertools.count())
    monkeypatch.setattr(store_module, "now_iso", lambda: next(ticks))
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    decided = await env.store.decide(PROJECT, apr.id, approve=True)

    await env.settle([decided])
    first = (await env.stored(apr.id)).settled_at
    await env.settle([decided])

    assert first is not None
    assert (await env.stored(apr.id)).settled_at == first


async def test_two_settles_of_one_approval_at_once_write_each_record_once(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    decided = await env.store.decide(PROJECT, apr.id, approve=True)
    slow_audit = SlowAuditLog(databases, secrets=[])
    slow_events = SlowEventLog(databases)

    await asyncio.gather(
        env.settle([decided], audit_log=slow_audit, event_log=slow_events),
        env.settle([decided], audit_log=slow_audit, event_log=slow_events),
    )

    assert (await env.audit_actors(apr.id), await env.event_sources(apr.id)) == (["user"], ["user"])
    assert (await env.card(apr)).status == "approved"
    assert (await env.stored(apr.id)).settled_at is not None


async def test_settle_rewrites_each_message_once_for_all_its_approvals(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    approvals = await env.tool_approvals(2)
    cancelled = await env.store.cancel_for_turn(PROJECT, "trn_1", decided_by="user")
    writes: list[tuple[str, str]] = []

    async def counting(
        session_id: str, message_id: str, transform: Callable[[list[Part]], list[Part]]
    ) -> object:
        writes.append((session_id, message_id))
        return await env.sessions.rewrite_parts(PROJECT, session_id, message_id, transform)

    await env.settle(cancelled, rewrite_part=counting)

    assert writes == [("ses_1", approvals[0].message_id)]
    assert [(await env.card(a)).status for a in approvals] == ["cancelled", "cancelled"]


# who decided


async def test_who_decided_is_stored_with_the_decision_and_is_the_records_actor(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    approve, reject, cancel = await env.tool_approvals(3)
    await env.refuse_a_decision_without_its_actor()
    service = ApprovalsService(
        databases,
        FakeSessionsForApproval(env.sessions),
        store=env.store,
        audit_log=env.audit_log,
        event_log=env.event_log,
    )

    await service.decide(PROJECT, approve.id, approve=True)
    await service.decide(PROJECT, reject.id, approve=False)
    await service.cancel(PROJECT, cancel.id)

    for apr, who in ((approve, "user"), (reject, "user"), (cancel, "system")):
        assert (await env.stored(apr.id)).decided_by == who
        assert await env.audit_actors(apr.id) == [who]
        assert await env.event_sources(apr.id) == [who]


async def test_the_sweeps_store_who_decided_in_the_same_update(databases: Databases) -> None:
    env = SettleEnv(databases)
    await env.tool_approvals(1, turn_id="trn_stop")
    await env.tool_approvals(1, turn_id="trn_stale", turn_status="interrupted")
    await env.refuse_a_decision_without_its_actor()

    [stopped] = await env.store.cancel_for_turn(PROJECT, "trn_stop", decided_by="user")
    [expired] = await env.store.expire_non_waiting(PROJECT)

    assert (stopped.status, stopped.decided_by) == ("cancelled", "user")
    assert (expired.status, expired.decided_by) == ("expired", "system")
    assert (await env.stored(expired.id)).decided_by == "system"


# the turn resumes only once every decision of it is settled


class HoldsOneDecision:
    """Holds the observers' `decided` of one approval until released: its status is committed,
    its card not written yet."""

    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        if view.id == self.approval_id:
            self.entered.set()
            await self.release.wait()


async def test_a_decision_does_not_resume_the_turn_while_another_of_it_is_not_settled(
    databases: Databases,
) -> None:
    # A resumed turn reads its message's parts once and writes them back: a card written after
    # the resume is lost, so the turn resumes only once every card of it is written.
    env = SettleEnv(databases)
    first, second = await env.tool_approvals(2)
    held = HoldsOneDecision(second.id)
    env.store.add_observer(held)
    sessions = FakeSessionsForApproval(env.sessions)
    service = ApprovalsService(databases, sessions, store=env.store, event_log=env.event_log)
    deciding_second = asyncio.create_task(service.decide(PROJECT, second.id, approve=True))
    await asyncio.wait_for(held.entered.wait(), timeout=5)

    await service.decide(PROJECT, first.id, approve=True)

    assert sessions.resumed_turns == []
    held.release.set()
    await deciding_second
    assert sessions.resumed_turns == [(PROJECT, "ses_1", "trn_1")]
    assert [(await env.card(a)).status for a in (first, second)] == ["approved", "approved"]


async def test_an_approval_with_no_known_actor_is_recorded_as_system(databases: Databases) -> None:
    env = SettleEnv(databases)
    [apr] = await env.tool_approvals(1)
    decided = await env.store.update_status(PROJECT, apr.id, "approved")
    assert decided.decided_by is None

    await env.settle([decided])

    assert await env.audit_actors(apr.id) == ["system"]
    assert await env.event_sources(apr.id) == ["system"]
