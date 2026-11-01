"""Events, audit and status (I11)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import Resync, TaskView
from mlops_agents.agents.tools.base import RiskClass

EventType = Literal[
    "turn.started", "turn.waiting", "turn.ended", "task.started", "task.ended",
    "pr.opened", "pr.updated", "issue.opened", "issue.commented", "issue.updated",
    "approval.requested", "approval.decided", "message.sent", "wakeup.scheduled",
    "sandbox.build.started", "sandbox.build.finished", "sandbox.build.failed",
    "connection.created", "connection.updated", "connection.deleted", "tracking.synced",
    "proposal.created", "proposal.decided", "handoff.approved", "turn.stalled", "turn.resumed",
    "note.saved", "note.deleted",
    "results.collected", "signal.raised", "incident.opened", "incident.updated",
    "incident.acknowledged", "incident.snoozed", "incident.severity_changed",
    "incident.reopened", "incident.merged", "incident.split", "incident.suspect_linked",
    "incident.hold_recorded", "incident.wake_skipped", "incident.closed",
    "check.registered", "parser.disabled", "notification.sent", "notification.failed",
    "escalation.sent", "report.sent", "hook.ran", "hook.failed", "hook.suspended",
    "rule.satisfied", "verdict.recorded", "miss.reported",
    "fix.requested", "fix.chosen", "fix.declined", "fix.escalated",
    "training.requested", "training.linked", "policy.suspended",
    "report.saved", "promotion.applied", "promotion.refused", "pr.merged_by_agent",
    "session.compacted", "session.cleared",
]  # fmt: skip

AuditDecision = Literal[
    "allowed", "asked", "denied", "approved", "rejected", "cancelled", "expired"
]
AgentState = Literal["idle", "running", "waiting_approval", "waiting_reply", "inactive"]


class EventRecord(BaseModel):
    id: str
    seq: int
    ts: str
    source: str  # agent name, "user" or "system"
    type: EventType
    subject_id: str | None
    payload: dict[str, Any]


class AuditRecord(BaseModel):
    id: str
    ts: str
    actor: str
    action: str  # the tool name, or "approval"
    risk_class: RiskClass
    permission_level: PermissionLevel
    decision: AuditDecision
    target: str
    detail: dict[str, Any]  # scrubbed, each value capped at 4,000 characters


class PrRef(BaseModel):
    number: int
    url: str
    title: str


LAST_FINISHED_MAX_CHARS: Final[int] = 200


class AgentStatus(BaseModel):
    agent: str
    display_name: str
    state: AgentState
    task: TaskView | None
    open_prs: list[PrRef]
    last_finished: str | None


class EventLog(Protocol):
    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord: ...

    def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]: ...

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        """Newest first."""
        ...

    async def page(
        self,
        project_id: str,
        *,
        subject_id: str | None = None,
        types: Sequence[EventType] | None = None,
        source: str | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        """Newest first, keyset on seq, before exclusive."""
        return []


class AuditLog(Protocol):
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
    ) -> AuditRecord: ...

    async def page(
        self,
        project_id: str,
        *,
        actor: str | None = None,
        before: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[AuditRecord]:
        """Newest first, keyset on (ts, id)."""
        ...


class StatusBoard(Protocol):
    async def snapshot(self, project_id: str) -> list[AgentStatus]: ...

    async def summary(self, project_id: str) -> str:
        """The compact text a status_board agent gets."""
        ...
