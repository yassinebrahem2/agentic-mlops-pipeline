"""Deciding a stall, and cancelling an approval (I13), through ApprovalsService."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, Part
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.errors import DomainError
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import EventType
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit
from tests.agents.approval_env import STALL_PREVIEW, add_turn
from tests.agents.test_approvals_service import FakeSessionsForApproval

PROJECT = "prj_stalldec"


class Env:
    def __init__(self, databases: Databases) -> None:
        self.databases = databases
        self.store = ApprovalStore(databases)
        self.events = SqliteEventLog(databases)
        self.sessions = FakeSessionsForApproval(SessionStore(databases))
        self.service = ApprovalsService(
            databases,
            self.sessions,
            store=self.store,
            audit_log=SqliteAuditLog(databases, secrets=[]),
            event_log=self.events,
        )

    async def audit_rows(self) -> list[dict[str, Any]]:
        engine = await self.databases.project(PROJECT)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(audit))).mappings().all()
        return [dict(r) for r in rows]

    async def event_payloads(self, event_type: EventType) -> list[dict[str, Any]]:
        return [e.payload for e in await self.events.history(PROJECT, types=[event_type])]

    async def part(self, approval_id: str) -> ApprovalPart:
        message = await self.sessions.store.turn_agent_message(PROJECT, "ses_1", "trn_1")
        assert message is not None
        return next(
            p for p in message.parts if isinstance(p, ApprovalPart) and p.approval_id == approval_id
        )


async def _stall_env(databases: Databases) -> tuple[Env, str]:
    """A waiting turn with a stall, and the stall's part in the turn's agent message."""
    message_id = await add_turn(databases, PROJECT)
    await set_permission_level(databases, PROJECT, "ask")
    env = Env(databases)
    stall = await env.service.create_stall(PROJECT, "trn_1", 2, STALL_PREVIEW)
    part = ApprovalPart(
        type="approval",
        approval_id=stall.id,
        tool_call_id=None,
        preview=STALL_PREVIEW,
        status="pending",
        note=None,
        kind="stall",
        step=2,
    )
    parts: list[Part] = [part]
    await env.sessions.store.update_message(PROJECT, message_id, parts=parts)
    return env, stall.id


@pytest.mark.asyncio
async def test_create_stall_records_approval_requested_once(databases: Databases) -> None:
    env, stall_id = await _stall_env(databases)

    again = await env.service.create_stall(PROJECT, "trn_1", 2, STALL_PREVIEW)

    assert again.id == stall_id
    assert again.kind == "stall"
    assert await env.event_payloads("approval.requested") == [
        {"approval_id": stall_id, "turn_id": "trn_1", "tool": None}
    ]


@pytest.mark.asyncio
async def test_approving_a_stall_resumes_the_turn_without_an_audit_record(
    databases: Databases,
) -> None:
    env, stall_id = await _stall_env(databases)

    view = await env.service.decide(PROJECT, stall_id, approve=True)

    assert view.status == "approved"
    assert env.sessions.resumed_turns == [(PROJECT, "ses_1", "trn_1")]
    assert env.sessions.stopped_turns == []
    assert await env.audit_rows() == []
    assert await env.event_payloads("approval.decided") == [
        {"approval_id": stall_id, "status": "approved"}
    ]
    part = await env.part(stall_id)
    assert (part.status, part.kind, part.step, part.tool_call_id) == ("approved", "stall", 2, None)


@pytest.mark.asyncio
async def test_rejecting_a_stall_stops_the_turn_without_an_audit_record(
    databases: Databases,
) -> None:
    env, stall_id = await _stall_env(databases)

    view = await env.service.decide(PROJECT, stall_id, approve=False)

    assert view.status == "rejected"
    assert env.sessions.stopped_turns == [(PROJECT, "ses_1", "trn_1")]
    assert env.sessions.resumed_turns == []
    assert await env.audit_rows() == []
    assert await env.event_payloads("approval.decided") == [
        {"approval_id": stall_id, "status": "rejected"}
    ]
    part = await env.part(stall_id)
    assert (part.status, part.kind, part.step) == ("rejected", "stall", 2)


@pytest.mark.asyncio
async def test_cancelling_and_expiring_a_stall_write_no_audit_record(
    databases: Databases,
) -> None:
    env, stall_id = await _stall_env(databases)

    await env.service.cancel(PROJECT, stall_id)

    assert await env.audit_rows() == []
    assert await env.event_payloads("approval.decided") == [
        {"approval_id": stall_id, "status": "cancelled"}
    ]
    assert (await env.part(stall_id)).status == "cancelled"

    # Expiry: recovery of a turn that is no longer waiting.
    await add_turn(databases, PROJECT, turn_id="trn_2", status="interrupted")
    stale, _ = await env.store.create_stall(PROJECT, "trn_2", 1, STALL_PREVIEW)
    await env.sessions.store.recover(
        PROJECT,
        approval_store=env.store,
        audit_log=SqliteAuditLog(databases, secrets=[]),
        event_log=env.events,
    )
    stored = await env.store.get(PROJECT, stale.id)
    assert stored is not None and stored.status == "expired"
    assert await env.audit_rows() == []
    assert {"approval_id": stale.id, "status": "expired"} in await env.event_payloads(
        "approval.decided"
    )


async def _tool_env(databases: Databases, calls: int) -> tuple[Env, list[str]]:
    """A waiting turn with `calls` pending tool approvals, each with its part."""
    message_id = await add_turn(databases, PROJECT)
    await set_permission_level(databases, PROJECT, "ask")
    env = Env(databases)
    ids: list[str] = []
    parts: list[Part] = []
    for n in range(calls):
        preview = Preview(kind="text", title="Propose", body="{}")
        record = await env.store.create(
            PROJECT,
            turn_id="trn_1",
            session_id="ses_1",
            message_id=message_id,
            agent="orchestrator",
            tool_call_id=f"call_{n}",
            tool="Propose",
            risk_class=RiskClass.PROPOSE,
            preview=preview,
            proposal_kind="connections",
        )
        ids.append(record.id)
        parts.append(
            ApprovalPart(
                type="approval",
                approval_id=record.id,
                tool_call_id=f"call_{n}",
                preview=preview,
                status="pending",
                note=None,
                proposal_kind="connections",
            )
        )
    await env.sessions.store.update_message(PROJECT, message_id, parts=parts)
    return env, ids


@pytest.mark.asyncio
async def test_cancelling_the_last_pending_approval_resumes_its_turn(
    databases: Databases,
) -> None:
    env, [approval_id] = await _tool_env(databases, 1)

    await env.service.cancel(PROJECT, approval_id)

    stored = await env.store.get(PROJECT, approval_id)
    assert stored is not None and stored.status == "cancelled"
    assert env.sessions.resumed_turns == [(PROJECT, "ses_1", "trn_1")]
    part = await env.part(approval_id)
    assert (part.status, part.kind, part.proposal_kind) == ("cancelled", "tool", "connections")
    [row] = await env.audit_rows()
    assert (row["decision"], row["risk_class"], row["target"]) == (
        "cancelled",
        "propose",
        "Propose",
    )
    assert await env.event_payloads("approval.decided") == [
        {"approval_id": approval_id, "status": "cancelled"}
    ]


@pytest.mark.asyncio
async def test_cancelling_one_of_two_pending_approvals_does_not_resume_the_turn(
    databases: Databases,
) -> None:
    env, [first, second] = await _tool_env(databases, 2)

    await env.service.cancel(PROJECT, first)

    assert env.sessions.resumed_turns == []
    still = await env.store.get(PROJECT, second)
    assert still is not None and still.status == "pending"


@pytest.mark.asyncio
async def test_cancelling_an_approval_that_is_not_pending_is_a_409(databases: Databases) -> None:
    env, [approval_id] = await _tool_env(databases, 1)
    await env.service.decide(PROJECT, approval_id, approve=True)

    with pytest.raises(DomainError) as err:
        await env.service.cancel(PROJECT, approval_id)

    assert (err.value.code, err.value.status) == ("approval_not_pending", 409)


@pytest.mark.asyncio
async def test_cancelling_an_unknown_approval_is_a_404(databases: Databases) -> None:
    env, _ = await _tool_env(databases, 1)

    with pytest.raises(DomainError) as err:
        await env.service.cancel(PROJECT, "apr_missing")

    assert (err.value.code, err.value.status) == ("approval_not_found", 404)


@pytest.mark.asyncio
async def test_a_decision_keeps_the_parts_kind_step_and_proposal_kind(
    databases: Databases,
) -> None:
    env, [approval_id] = await _tool_env(databases, 1)

    await env.service.decide(PROJECT, approval_id, approve=False, note="not now")

    part = await env.part(approval_id)
    assert (part.status, part.note, part.kind, part.step, part.proposal_kind) == (
        "rejected",
        "not now",
        "tool",
        None,
        "connections",
    )
