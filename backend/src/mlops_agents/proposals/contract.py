"""Proposals (I19) and the current handoff (I20): their types and protocols.

`ProposalKinds` and `SqliteHandoffs` belong to the proposals plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload

# The one source of the kind names.
CONNECTIONS_KIND: Final = "connections"
HANDOFF_KIND: Final = "handoff"

ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]


class CredentialSlot(BaseModel):
    index: int
    label: str
    source_type: str


class SlotState(BaseModel):
    index: int
    label: str
    set: bool


class ApplyResult(BaseModel):
    applied: list[str]
    failed: dict[str, str]  # part -> reason


class ProposalView(BaseModel):
    id: str
    kind: str
    key: str
    # Typed so the cards read it from the generated schema. The two members share no required
    # field, so the union is unambiguous.
    payload: ConnectionsPayload | HandoffPayload
    warnings: list[str]
    status: ProposalStatus
    agent: str
    approval_id: str | None
    turn_id: str
    tool_call_id: str
    result: ApplyResult | None
    slots: list[SlotState]
    created: datetime


class ProposalKind(Protocol):
    """Each kind: preview, warnings, credentials_needed and apply by onboarding; its card by proposals."""

    name: str
    payload_model: type[BaseModel]

    def key(self, payload: BaseModel) -> str:
        """The superseding key within the project: constant for connections and handoff."""
        ...

    def preview(self, payload: BaseModel) -> Preview: ...

    def warnings(self, payload: BaseModel) -> list[str]:
        """User-facing text; the card shows it as given."""
        ...

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]: ...

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        """Idempotent."""
        ...


class Proposals(Protocol):
    async def create(
        self,
        project_id: str,
        agent: str,
        kind: str,
        key: str,
        payload: dict[str, Any],
        turn_id: str,
        tool_call_id: str,
    ) -> ProposalView:
        """A draft, idempotent by (turn_id, tool_call_id); approval_id is None until attached."""
        ...

    async def attach(self, project_id: str, proposal_id: str, approval_id: str) -> str | None:
        """Called on asked(view); returns the approval ID of a pending proposal it supersedes."""
        ...

    async def get_by_approval(self, project_id: str, approval_id: str) -> ProposalView | None: ...

    async def pending(self, project_id: str, kind: str, key: str) -> ProposalView | None:
        """Superseding is by kind and key."""
        ...

    async def latest_for_turn(
        self, project_id: str, turn_id: str, kind: str
    ) -> ProposalView | None: ...


class HandoffView(BaseModel):
    proposal_id: str
    version: int
    approved: datetime
    payload: HandoffPayload


class Handoffs(Protocol):
    async def current(self, project_id: str) -> HandoffView | None: ...

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        """Idempotent: one version per proposal; returns that version."""
        ...
