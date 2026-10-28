"""The stall (stage 3 spec, section 2): a model step whose provider has no usable key pauses the
turn on a `stall` approval, and Try again (approving it) resumes the turn from its checkpoint.

The model node records the stall (`record_stall`), the wait node waits on it (`await_stall`),
and the turn's waiting card is built here (`stall_part`)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from langgraph.types import interrupt

from mlops_agents.agents.approvals.contract import ApprovalObserver, Approvals, ApprovalView
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelKeysExhausted
from mlops_agents.agents.tools.base import Preview
from mlops_agents.events.contract import EventLog

__all__ = [
    "STALL_TITLE",
    "LateApprovals",
    "StallState",
    "await_stall",
    "pending_stall",
    "record_stall",
    "stall_part",
    "stall_preview",
]

STALL_TITLE = "Stalled: no usable key"


@dataclass(frozen=True)
class StallState:
    """The turn's stall, in its checkpointed state: pending until Try again resolves it."""

    approval_id: str
    provider_id: str
    model: str
    step: int
    preview: Preview
    resolved: bool = False


def pending_stall(stall: StallState | None) -> StallState | None:
    """The stall the turn waits on: one set and not yet resolved."""
    return stall if stall is not None and not stall.resolved else None


def stall_preview(exhausted: ModelKeysExhausted) -> Preview:
    return Preview(kind="text", title=STALL_TITLE, body=exhausted.description)


def stall_part(stall: StallState) -> ApprovalPart:
    """The stall's card in the turn's agent message."""
    return ApprovalPart(
        type="approval",
        approval_id=stall.approval_id,
        tool_call_id=None,
        preview=stall.preview,
        status="pending",
        note=None,
        kind="stall",
        step=stall.step,
        proposal_kind=None,
    )


async def record_stall(
    approvals: Approvals,
    events: EventLog | None,
    *,
    project_id: str,
    agent: str,
    turn_id: str,
    step: int,
    exhausted: ModelKeysExhausted,
) -> StallState:
    """The model node's stall: the step's pending stall approval, found again on a re-run of the
    node, and its `turn.stalled` event. A re-run (a crash before the checkpoint) records the
    event again: it is informational."""
    approval = await approvals.create_stall(project_id, turn_id, step, stall_preview(exhausted))
    if events is not None:
        await events.record(
            project_id,
            source=agent,
            type="turn.stalled",
            subject_id=turn_id,
            payload={
                "turn_id": turn_id,
                "provider": exhausted.provider_id,
                "model": exhausted.model,
            },
        )
    return StallState(
        approval_id=approval.id,
        provider_id=exhausted.provider_id,
        model=exhausted.model,
        step=step,
        preview=approval.preview,
    )


async def await_stall(
    stall: StallState,
    *,
    store: ApprovalStore,
    caller: ModelCaller,
    events: EventLog | None,
    project_id: str,
    agent: str,
    turn_id: str,
) -> dict[str, Any]:
    """The wait node on a pending stall: pause while it is pending; once approved (Try again),
    forget the provider's key states and mark the stall resolved, so the graph calls the model
    again. A crash after the reset and before the checkpoint records `turn.resumed` again."""
    approval = await store.get(project_id, stall.approval_id)
    status = None if approval is None else approval.status
    if status == "pending":
        # LangGraph runs this node again on resume: nothing before here may change anything.
        interrupt(None)
    if status != "approved":
        # Stop and a rejected stall end the turn without resuming it; recovery expires only
        # the approvals of turns that do not wait.
        raise RuntimeError(
            f"The turn {turn_id} resumed on its stall {stall.approval_id}, which is "
            f"{status or 'missing'}: only an approved stall resumes a turn."
        )
    await caller.reset_keys(stall.provider_id)
    if events is not None:
        await events.record(
            project_id,
            source=agent,
            type="turn.resumed",
            subject_id=turn_id,
            payload={"turn_id": turn_id},
        )
    return {"stall": replace(stall, resolved=True)}


class LateApprovals(Approvals):
    """The turn hooks' approvals. The approvals service takes the session manager, which builds
    the turn hooks, so the service is bound here once it exists (`SessionManager.bind_approvals`)."""

    def __init__(self) -> None:
        self._approvals: Approvals | None = None

    def bind(self, approvals: Approvals) -> None:
        """A second call raises RuntimeError."""
        if self._approvals is not None:
            raise RuntimeError("The turn hooks' approvals are already bound.")
        self._approvals = approvals

    def _bound(self) -> Approvals:
        if self._approvals is None:
            raise RuntimeError("No approvals service is bound to the turn hooks.")
        return self._approvals

    async def pending(self, project_id: str) -> list[ApprovalView]:
        return await self._bound().pending(project_id)

    async def decide(
        self, project_id: str, approval_id: str, *, approve: bool, note: str | None
    ) -> ApprovalView:
        return await self._bound().decide(project_id, approval_id, approve=approve, note=note)

    async def create_stall(
        self, project_id: str, turn_id: str, step: int, preview: Preview
    ) -> ApprovalView:
        return await self._bound().create_stall(project_id, turn_id, step, preview)

    async def cancel(self, project_id: str, approval_id: str) -> None:
        await self._bound().cancel(project_id, approval_id)

    def add_observer(self, observer: ApprovalObserver) -> None:
        self._bound().add_observer(observer)
