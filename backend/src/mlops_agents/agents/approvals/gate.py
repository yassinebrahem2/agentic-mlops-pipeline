"""The ApprovalGate implementation (I12)."""

from __future__ import annotations

from collections.abc import Sequence

from langgraph.types import interrupt
from pydantic import BaseModel

from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.policy.table import decide as policy_decide
from mlops_agents.agents.runtime.hooks import Gate, GateCall, GateDecision
from mlops_agents.agents.runtime.tool_step import REJECTED_TEXT
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases


def format_rejection_message(note: str | None) -> str:
    """Format the tool error for a user rejection, appending the note if given."""
    if note and note.strip():
        return f"{REJECTED_TEXT} Note: {note.strip()}"
    return REJECTED_TEXT


def denial_message(level: PermissionLevel, risk: RiskClass) -> str:
    """The tool error for a policy denial."""
    if level == "suggest" and risk == RiskClass.WRITE_CODE:
        return (
            "Action denied: permission level is 'suggest'. "
            "Propose the change as a diff in your answer instead of modifying files directly."
        )
    return f"Action denied: permission level '{level}' denies '{risk.value}' actions."


def _extract_target(tool_name: str, args: BaseModel) -> str:
    for attr in ("path", "file", "file_path", "command", "url", "title", "target"):
        if hasattr(args, attr):
            val = getattr(args, attr)
            if val is not None and str(val).strip():
                return str(val)
    return tool_name


def _proposal_kind(call: GateCall) -> str | None:
    """The kind a call of a tool of class propose proposes: its `kind` argument when that is a
    string. The risk class decides, never the tool's name."""
    if call.tool.risk != RiskClass.PROPOSE:
        return None
    kind: object = getattr(call.args, "kind", None)
    return kind if isinstance(kind, str) else None


class ApprovalGate(Gate):
    """The gate enforcing permission policy on every tool call."""

    def __init__(
        self,
        databases: Databases | None = None,
        *,
        store: ApprovalStore,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._databases = databases
        self._store = store
        self._audit_log = audit_log
        self._event_log = event_log

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        """Never pauses. Evaluates policy for each call, logs audit/events, and creates approval requests."""
        results: dict[str, GateDecision] = {}
        if not calls:
            return results

        # Cache level lookups per project_id
        level_cache: dict[str, PermissionLevel] = {}

        for call in calls:
            project_id = call.ctx.project_id
            if project_id not in level_cache:
                if self._databases is not None:
                    level_cache[project_id] = await get_permission_level(
                        self._databases, project_id
                    )
                else:
                    level_cache[project_id] = "ask"

            level = level_cache[project_id]
            risk = call.tool.risk
            policy_dec = policy_decide(risk, level)

            target = _extract_target(call.tool.name, call.args)
            detail = call.args.model_dump()

            if policy_dec == "allow":
                if self._audit_log is not None:
                    await self._audit_log.record(
                        project_id,
                        actor=call.ctx.agent,
                        action=call.tool.name,
                        risk_class=risk,
                        permission_level=level,
                        decision="allowed",
                        target=target,
                        detail=detail,
                    )
                results[call.tool_call_id] = GateDecision(
                    outcome="allow", message=None, approval_id=None, preview=None
                )

            elif policy_dec == "deny":
                msg = denial_message(level, risk)
                if self._audit_log is not None:
                    await self._audit_log.record(
                        project_id,
                        actor=call.ctx.agent,
                        action=call.tool.name,
                        risk_class=risk,
                        permission_level=level,
                        decision="denied",
                        target=target,
                        detail=detail,
                    )
                results[call.tool_call_id] = GateDecision(
                    outcome="deny", message=msg, approval_id=None, preview=None
                )

            elif policy_dec == "ask":
                if self._audit_log is not None:
                    await self._audit_log.record(
                        project_id,
                        actor=call.ctx.agent,
                        action=call.tool.name,
                        risk_class=risk,
                        permission_level=level,
                        decision="asked",
                        target=target,
                        detail=detail,
                    )

                preview = await call.tool.preview(call.ctx, call.args)
                task_id = call.ctx.task.id if call.ctx.task is not None else None
                proposal_kind = _proposal_kind(call)
                approval = await self._store.create(
                    project_id,
                    turn_id=call.ctx.turn_id,
                    session_id=call.ctx.session_id,
                    message_id=call.message_id,
                    agent=call.ctx.agent,
                    tool_call_id=call.tool_call_id,
                    tool=call.tool.name,
                    risk_class=risk,
                    preview=preview,
                    task_id=task_id,
                    proposal_kind=proposal_kind,
                )

                if self._event_log is not None:
                    await self._event_log.record(
                        project_id,
                        source=call.ctx.agent,
                        type="approval.requested",
                        subject_id=approval.id,
                        payload={
                            "approval_id": approval.id,
                            "turn_id": call.ctx.turn_id,
                            "tool": call.tool.name,
                        },
                    )

                results[call.tool_call_id] = GateDecision(
                    outcome="ask",
                    message=None,
                    approval_id=approval.id,
                    preview=preview,
                    proposal_kind=proposal_kind,
                )

        return results

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        """Calls interrupt() while an approval is pending. Maps decided ones to approved/rejected."""
        approval_ids = [d.approval_id for d in decisions.values() if d.approval_id is not None]
        if not approval_ids:
            return decisions

        approvals = await self._store.list_for_step(project_id, turn_id, approval_ids=approval_ids)

        if any(a.status == "pending" for a in approvals):
            interrupt(None)

        apr_map = {a.id: a for a in approvals}

        updated_decisions: dict[str, GateDecision] = dict(decisions)
        for tool_call_id, dec in decisions.items():
            if dec.approval_id and dec.approval_id in apr_map:
                apr = apr_map[dec.approval_id]
                if apr.status == "approved":
                    updated_decisions[tool_call_id] = GateDecision(
                        outcome="approved",
                        message=None,
                        approval_id=apr.id,
                        preview=apr.preview,
                    )
                elif apr.status in ("rejected", "cancelled", "expired"):
                    updated_decisions[tool_call_id] = GateDecision(
                        outcome="rejected",
                        message=format_rejection_message(apr.note),
                        approval_id=apr.id,
                        preview=apr.preview,
                    )

        return updated_decisions
