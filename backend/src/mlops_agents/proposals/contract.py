"""Proposals (I19) and the current handoff (I20): their types and protocols.

`ProposalKinds` and `SqliteHandoffs` belong to the proposals plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, RootModel

if TYPE_CHECKING:
    from mlops_agents.agents.tools.base import Preview
from mlops_agents.monitoring.hooks.contract import HooksPayload
from mlops_agents.monitoring.incidents.contract import MaintenancePayload
from mlops_agents.monitoring.results.contract import ParserPayload
from mlops_agents.monitoring.rules.contract import RulePayload
from mlops_agents.promotion.contract import PromotionPayload
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload
from mlops_agents.training.contract import TrainingPolicyPayload

# The one source of the kind names (I32).
CONNECTIONS_KIND: Final = "connections"
HANDOFF_KIND: Final = "handoff"
PARSER_KIND: Final = "parser"
RULE_KIND: Final = "rule"
HOOKS_KIND: Final = "hooks"
MAINTENANCE_KIND: Final = "maintenance"
TRAINING_POLICY_KIND: Final = "training_policy"
PROMOTION_KIND: Final = "promotion"

ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]


def connections_key(payload: ConnectionsPayload) -> str:
    return "project"


def handoff_key(payload: HandoffPayload) -> str:
    return "project"


def parser_key(payload: ParserPayload) -> str:
    return payload.spec.source_id


def rule_key(payload: RulePayload) -> str:
    return f"{payload.spec.model}:{payload.spec.name}" if payload.spec.model else payload.spec.name


def hooks_key(payload: HooksPayload) -> str:
    return "project"


def maintenance_key(payload: MaintenancePayload) -> str:
    return f"{payload.window.reason}:{payload.window.start.isoformat()}"


def training_policy_key(payload: TrainingPolicyPayload) -> str:
    return payload.model


def promotion_key(payload: PromotionPayload) -> str:
    return payload.model


class CredentialSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    label: str
    source_type: str


class SlotState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    label: str
    set: bool


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: list[str]
    failed: dict[str, str]  # part -> reason


class BaseProposalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    key: str
    warnings: list[str]
    status: ProposalStatus
    agent: str
    approval_id: str | None
    turn_id: str
    tool_call_id: str
    result: ApplyResult | None
    slots: list[SlotState]
    created: datetime


class ConnectionsProposalView(BaseProposalView):
    kind: Literal["connections"] = CONNECTIONS_KIND
    payload: ConnectionsPayload


class HandoffProposalView(BaseProposalView):
    kind: Literal["handoff"] = HANDOFF_KIND
    payload: HandoffPayload


class ParserProposalView(BaseProposalView):
    kind: Literal["parser"] = PARSER_KIND
    payload: ParserPayload


class RuleProposalView(BaseProposalView):
    kind: Literal["rule"] = RULE_KIND
    payload: RulePayload


class HooksProposalView(BaseProposalView):
    kind: Literal["hooks"] = HOOKS_KIND
    payload: HooksPayload


class MaintenanceProposalView(BaseProposalView):
    kind: Literal["maintenance"] = MAINTENANCE_KIND
    payload: MaintenancePayload


class TrainingPolicyProposalView(BaseProposalView):
    kind: Literal["training_policy"] = TRAINING_POLICY_KIND
    payload: TrainingPolicyPayload


class PromotionProposalView(BaseProposalView):
    kind: Literal["promotion"] = PROMOTION_KIND
    payload: PromotionPayload


AnyProposalView = Annotated[
    ConnectionsProposalView
    | HandoffProposalView
    | ParserProposalView
    | RuleProposalView
    | HooksProposalView
    | MaintenanceProposalView
    | TrainingPolicyProposalView
    | PromotionProposalView,
    Field(discriminator="kind"),
]


class ProposalView(RootModel[AnyProposalView]):
    def __init__(self, root: AnyProposalView | Any = None, **kwargs: Any) -> None:
        if root is not None:
            super().__init__(root=cast(AnyProposalView, root))  # pyright: ignore[reportUnknownMemberType]  # Pydantic RootModel.__init__ lacks complete type stubs
        else:
            super().__init__(root=cast(AnyProposalView, kwargs))  # pyright: ignore[reportUnknownMemberType]  # Pydantic RootModel.__init__ lacks complete type stubs

    @property
    def id(self) -> str:
        return self.root.id

    @property
    def kind(self) -> str:
        return self.root.kind

    @property
    def key(self) -> str:
        return self.root.key

    @property
    def payload(self) -> Any:
        return self.root.payload

    @property
    def warnings(self) -> list[str]:
        return self.root.warnings

    @property
    def status(self) -> ProposalStatus:
        return self.root.status

    @property
    def agent(self) -> str:
        return self.root.agent

    @property
    def approval_id(self) -> str | None:
        return self.root.approval_id

    @property
    def turn_id(self) -> str:
        return self.root.turn_id

    @property
    def tool_call_id(self) -> str:
        return self.root.tool_call_id

    @property
    def result(self) -> ApplyResult | None:
        return self.root.result

    @property
    def slots(self) -> list[SlotState]:
        return self.root.slots

    @property
    def created(self) -> datetime:
        return self.root.created

    def __getattr__(self, name: str) -> Any:
        if name in ("root", "__dict__", "__pydantic_fields_set__", "__pydantic_extra__"):
            raise AttributeError(name)
        return getattr(self.root, name)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> ProposalView:
        if update is not None and "root" not in update:
            return ProposalView(root=self.root.model_copy(update=dict(update), deep=deep))
        return super().model_copy(update=update, deep=deep)


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

    async def apply_allowed(self, project_id: str, proposal_id: str) -> ApplyResult | None:
        """Applies an allowed proposal at once with no approval."""
        ...


@runtime_checkable
class ProposalCheck(Protocol):
    """Refusals returned at propose time before the gate."""

    async def check(self, project_id: str, payload: Any) -> list[str]: ...


class HandoffView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    version: int
    approved: datetime
    payload: HandoffPayload


class HandoffObserver(Protocol):
    async def stored(self, project_id: str, handoff: HandoffView) -> None: ...


class Handoffs(Protocol):
    def add_observer(self, observer: HandoffObserver) -> None: ...

    async def current(self, project_id: str) -> HandoffView | None: ...

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        """Idempotent: one version per proposal; returns that version."""
        ...
