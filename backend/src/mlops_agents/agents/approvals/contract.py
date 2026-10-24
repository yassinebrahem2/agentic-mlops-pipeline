"""Approvals (I12)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import ApprovalStatus
from mlops_agents.agents.tools.base import Preview, RiskClass


class ApprovalView(BaseModel):
    id: str
    agent: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool: str
    risk_class: RiskClass
    preview: Preview
    status: ApprovalStatus
    note: str | None
    created_at: str
    decided_at: str | None


class Approvals(Protocol):
    async def pending(self, project_id: str) -> list[ApprovalView]: ...

    async def decide(
        self, project_id: str, approval_id: str, *, approve: bool, note: str | None
    ) -> ApprovalView: ...
