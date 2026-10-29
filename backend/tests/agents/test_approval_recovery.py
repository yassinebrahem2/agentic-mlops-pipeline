"""The parts of the restart repair below the session stack: one recovery lock per project (#380
item 3), the store's reads of what to repair, and `stop_turn`, which ends a rejected stall's turn
without the chat check."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import event

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, Part
from mlops_agents.agents.runtime.repair import ProjectRecovery
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.approval_env import STALL_PREVIEW, TOOL_PREVIEW, add_turn
from tests.agents.repair_env import QUIET, HeldObserver, RepairEnv
from tests.agents.settle_env import PROJECT, SettleEnv


async def _expiring_approval(databases: Databases, store: ApprovalStore, project_id: str) -> str:
    """A pending approval of a turn left running: the project's recovery expires it."""
    message_id = await add_turn(databases, project_id, status="running")
    record = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id=message_id,
        agent="orchestrator",
        tool_call_id="call_1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=TOOL_PREVIEW,
    )
    return record.id


async def test_a_slow_recovery_of_one_project_does_not_delay_another(
    databases: Databases, paths: DataPaths
) -> None:
    for project_id in ("prj_a", "prj_b"):
        paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    store = ApprovalStore(databases)
    sessions = SessionStore(databases)
    expiring = await _expiring_approval(databases, store, "prj_a")
    held = HeldObserver([expiring])
    store.add_observer(held)
    finished: list[str] = []

    async def finish(project_id: str, session_id: str, turn_id: str) -> None:
        finished.append(turn_id)

    recovery = ProjectRecovery(
        store=sessions,
        approval_store=store,
        databases=databases,
        audit_log=None,
        event_log=None,
        rewrite_part=sessions.rewrite_parts,
        stop_turn=finish,
        resume_turn=finish,
    )
    first_a = asyncio.create_task(recovery.ensure("prj_a"))
    await held.wait_entered(expiring)  # project A's recovery holds its lock, in its expiry

    await asyncio.wait_for(recovery.ensure("prj_b"), timeout=5)

    assert recovery.recovered() == {"prj_b"}
    second_a = asyncio.create_task(recovery.ensure("prj_a"))
    done, _ = await asyncio.wait({second_a}, timeout=0.2)
    assert not done  # the same project's second caller waits for its first recovery
    held.release[expiring].set()
    await asyncio.gather(first_a, second_a)
    assert recovery.recovered() == {"prj_a", "prj_b"}
    assert held.finished == [expiring]
    assert finished == []


async def test_unsettled_lists_decided_approvals_not_settled_through_its_index(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    pending, cut, settled = await env.tool_approvals(3)
    cut = await env.store.decide(PROJECT, cut.id, approve=True)
    await env.settle([await env.store.decide(PROJECT, settled.id, approve=False)])
    engine = await databases.project(PROJECT)
    selects: list[tuple[str, Any]] = []

    def capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        found = await env.store.unsettled(PROJECT)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert [(r.id, r.status) for r in found] == [(cut.id, "approved")]
    assert pending.id not in {r.id for r in found}
    [(statement, parameters)] = selects
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
        plan = "\n".join(str(detail) for *_, detail in rows)
    assert "approvals_unsettled" in plan, plan


async def test_of_waiting_turns_lists_every_approval_of_a_waiting_turn(
    databases: Databases,
) -> None:
    env = SettleEnv(databases)
    first, second = await env.tool_approvals(2, turn_id="trn_wait")
    await env.tool_approvals(1, turn_id="trn_done", turn_status="done")
    await env.store.decide(PROJECT, first.id, approve=True)

    found = await env.store.of_waiting_turns(PROJECT)

    assert [(r.id, r.status) for r in found] == [(first.id, "approved"), (second.id, "pending")]


async def _chatless_stall(repair: RepairEnv) -> tuple[str, str]:
    """A waiting turn of the chat: false agent on a stall, with its card; (stall, message)."""
    message_id = await add_turn(
        repair.databases,
        repair.project_id,
        session_id="ses_quiet",
        turn_id="trn_quiet",
        agent=QUIET,
    )
    stall, _ = await repair.first.store.create_stall(
        repair.project_id, "trn_quiet", 1, STALL_PREVIEW
    )
    card: list[Part] = [
        ApprovalPart(
            type="approval",
            approval_id=stall.id,
            tool_call_id=None,
            preview=STALL_PREVIEW,
            status="pending",
            note=None,
            kind="stall",
            step=1,
        )
    ]
    await repair.sessions.update_message(repair.project_id, message_id, parts=card)
    return stall.id, message_id


async def test_rejecting_the_stall_of_a_chatless_agent_stops_its_turn(repair: RepairEnv) -> None:
    stall_id, message_id = await _chatless_stall(repair)

    await repair.first.service.decide(repair.project_id, stall_id, approve=False, note=None)

    assert await repair.turn_status("trn_quiet") == "stopped"
    message = await repair.sessions.message(repair.project_id, "ses_quiet", message_id)
    assert message is not None and message.status == "stopped"
    assert [(p.approval_id, p.status) for p in message.parts if isinstance(p, ApprovalPart)] == [
        (stall_id, "rejected")
    ]
    with pytest.raises(DomainError) as refused:  # the user's Stop keeps the chat check
        await repair.first.manager.stop(repair.project_id, QUIET)
    assert (refused.value.code, refused.value.status) == ("agent_cannot_chat", 409)


async def test_stop_turn_leaves_a_turn_that_is_not_waiting_alone(repair: RepairEnv) -> None:
    message_id = await add_turn(
        repair.databases,
        repair.project_id,
        session_id="ses_quiet",
        turn_id="trn_done",
        agent=QUIET,
        status="done",
    )
    await add_turn(
        repair.databases,
        repair.project_id,
        session_id="ses_quiet",
        turn_id="trn_waits",
        agent=QUIET,
    )

    await repair.first.manager.stop_turn(repair.project_id, "ses_quiet", "trn_done")

    assert await repair.turn_status("trn_done") == "done"
    assert await repair.turn_status("trn_waits") == "waiting"
    message = await repair.sessions.message(repair.project_id, "ses_quiet", message_id)
    assert message is not None and message.status == "complete"
