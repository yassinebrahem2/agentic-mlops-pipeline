"""Approvals (I12, I13)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import ApprovalKind, ApprovalStatus
from mlops_agents.agents.tools.base import Preview, RiskClass


class ApprovalView(BaseModel):
    id: str
    agent: str
    session_id: str
    turn_id: str
    kind: ApprovalKind
    tool_call_id: str | None  # tool, tool_call_id and risk_class are None for a stall
    tool: str | None
    risk_class: RiskClass | None
    step: int | None  # a stall's model step
    proposal_kind: str | None  # the call's kind, for a tool of class propose
    preview: Preview
    status: ApprovalStatus
    note: str | None
    created_at: str
    decided_at: str | None


class ApprovalObserver(Protocol):
    """Told of every approval asked, and of every move out of pending (approve, reject,
    cancel, Stop, expiry), each after it is stored. An observer must not block. Two paths hold
    a lock while they tell `decided`: Stop (the session lock) and recovery expiry (the recovery
    lock); both only reject or cancel, and an observer must not call back into Approvals or
    AgentSessions from them. The approve path holds no lock, so an observer may post messages
    there (onboarding's kickoff does)."""

    async def asked(self, view: ApprovalView) -> None: ...

    async def decided(self, view: ApprovalView) -> None: ...


class Approvals(Protocol):
    async def pending(self, project_id: str) -> list[ApprovalView]: ...

    async def decide(
        self, project_id: str, approval_id: str, *, approve: bool, note: str | None
    ) -> ApprovalView: ...

    async def create_stall(
        self, project_id: str, turn_id: str, step: int, preview: Preview
    ) -> ApprovalView:
        """Idempotent: one pending stall per turn and step."""
        ...

    async def cancel(self, project_id: str, approval_id: str) -> None:
        """Marks a pending approval cancelled (a superseded proposal)."""
        ...

    def add_observer(self, observer: ApprovalObserver) -> None: ...
