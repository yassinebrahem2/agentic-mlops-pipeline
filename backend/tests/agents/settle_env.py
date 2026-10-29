"""A project with pending tool approvals and their cards, and logs that are slow or cancelled,
for the settle tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import func, select

from mlops_agents.agents.approvals.decisions import (
    PartRewriter,
    record_decisions,
    settle_decisions,
)
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import ApprovalPart, Part
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import (
    AuditDecision,
    AuditLog,
    AuditRecord,
    EventLog,
    EventRecord,
    EventType,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit, events
from tests.agents.approval_env import TOOL_PREVIEW, add_turn

PROJECT = "prj_settle"

# Test-only: a decision that leaves decided_by NULL aborts, so who decided must be written by
# the same UPDATE that moves the status out of pending.
_REFUSE_DECISION_WITHOUT_ACTOR = """
CREATE TRIGGER refuse_decision_without_actor AFTER UPDATE OF status ON approvals
WHEN NEW.status <> 'pending' AND NEW.decided_by IS NULL
BEGIN SELECT RAISE(ABORT, 'decided without decided_by'); END
"""


class SlowEventLog(SqliteEventLog):
    """Yields before every write, so two settles of one approval interleave their checks."""

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        await asyncio.sleep(0.02)
        return await super().record(
            project_id, source=source, type=type, payload=payload, subject_id=subject_id
        )


class SlowAuditLog(SqliteAuditLog):
    async def record(
        self,
        project_id: str,
        *,
        actor: str,
        action: str,
        risk_class: RiskClass,
        permission_level: PermissionLevel,
        decision: AuditDecision,
        target: str,
        detail: dict[str, Any],
    ) -> AuditRecord:
        await asyncio.sleep(0.02)
        return await super().record(
            project_id,
            actor=actor,
            action=action,
            risk_class=risk_class,
            permission_level=permission_level,
            decision=decision,
            target=target,
            detail=detail,
        )


class CancelledOnceEventLog(SqliteEventLog):
    """Its first write is cancelled, as a process dying there would leave it."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self.calls = 0

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        self.calls += 1
        if self.calls == 1:
            raise asyncio.CancelledError
        return await super().record(
            project_id, source=source, type=type, payload=payload, subject_id=subject_id
        )


async def cancelled_rewrite(
    session_id: str, message_id: str, transform: Callable[[list[Part]], list[Part]]
) -> object:
    raise asyncio.CancelledError


class SettleEnv:
    """One approval store, session store and pair of logs on a project, and what they hold."""

    def __init__(self, databases: Databases) -> None:
        self.databases = databases
        self.store = ApprovalStore(databases)
        self.sessions = SessionStore(databases)
        self.audit_log: AuditLog = SqliteAuditLog(databases, secrets=[])
        self.event_log: EventLog = SqliteEventLog(databases)

    async def tool_approvals(
        self, count: int, *, turn_id: str = "trn_1", turn_status: str = "waiting"
    ) -> list[ApprovalRecord]:
        """`count` pending tool approvals of a new turn, each with its card in the message."""
        ids = [f"apr_{turn_id}_{n}" for n in range(count)]
        parts: list[Part] = [
            ApprovalPart(
                type="approval",
                approval_id=i,
                tool_call_id=f"call_{i}",
                preview=TOOL_PREVIEW,
                status="pending",
                note=None,
            )
            for i in ids
        ]
        message_id = await add_turn(
            self.databases, PROJECT, turn_id=turn_id, status=turn_status, parts=parts
        )
        return [
            await self.store.create(
                PROJECT,
                id=i,
                turn_id=turn_id,
                session_id="ses_1",
                message_id=message_id,
                agent="orchestrator",
                tool_call_id=f"call_{i}",
                tool="edit_file",
                risk_class=RiskClass.WRITE_CODE,
                preview=TOOL_PREVIEW,
            )
            for i in ids
        ]

    async def settle(
        self,
        approvals: Sequence[ApprovalRecord],
        *,
        rewrite_part: PartRewriter | None = None,
        event_log: EventLog | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        async def stored_rewrite(
            session_id: str, message_id: str, transform: Callable[[list[Part]], list[Part]]
        ) -> object:
            return await self.sessions.rewrite_parts(PROJECT, session_id, message_id, transform)

        await settle_decisions(
            PROJECT,
            approvals,
            store=self.store,
            databases=self.databases,
            audit_log=audit_log or self.audit_log,
            event_log=event_log or self.event_log,
            rewrite_part=rewrite_part or stored_rewrite,
        )

    async def record(self, approval: ApprovalRecord, *, event_log: EventLog | None) -> None:
        await record_decisions(
            PROJECT,
            [approval],
            "approved",
            actor="user",
            databases=self.databases,
            audit_log=self.audit_log,
            event_log=event_log,
        )

    async def audit_actors(self, approval_id: str) -> list[str]:
        engine = await self.databases.project(PROJECT)
        query = select(audit.c.actor).where(
            func.json_extract(audit.c.detail, "$.approval_id") == approval_id
        )
        async with engine.connect() as conn:
            return [str(a) for a in (await conn.execute(query)).scalars()]

    async def event_sources(self, approval_id: str) -> list[str]:
        engine = await self.databases.project(PROJECT)
        query = select(events.c.source).where(
            events.c.type == "approval.decided", events.c.subject_id == approval_id
        )
        async with engine.connect() as conn:
            return [str(s) for s in (await conn.execute(query)).scalars()]

    async def card(self, approval: ApprovalRecord) -> ApprovalPart:
        message = await self.sessions.message(PROJECT, approval.session_id, approval.message_id)
        assert message is not None
        return next(
            p for p in message.parts if isinstance(p, ApprovalPart) and p.approval_id == approval.id
        )

    async def stored(self, approval_id: str) -> ApprovalRecord:
        record = await self.store.get(PROJECT, approval_id)
        assert record is not None
        return record

    async def refuse_a_decision_without_its_actor(self) -> None:
        engine = await self.databases.project(PROJECT)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_REFUSE_DECISION_WITHOUT_ACTOR)
