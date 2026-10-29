"""The Propose tool (I19): lets agents propose configuration changes for user approval."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import CONNECTIONS_KIND, HANDOFF_KIND
from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload

if TYPE_CHECKING:
    from mlops_agents.proposals.kinds import ProposalKinds
    from mlops_agents.proposals.service import ProposalsService

PROPOSE_TOOL_NAME: Final = "Propose"

_KIND_PAYLOAD_MODELS: Final[dict[str, type[BaseModel]]] = {
    CONNECTIONS_KIND: ConnectionsPayload,
    HANDOFF_KIND: HandoffPayload,
}


class ProposeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    payload: ConnectionsPayload | HandoffPayload

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in (CONNECTIONS_KIND, HANDOFF_KIND):
            raise ValueError(
                f"kind must be '{CONNECTIONS_KIND}' or '{HANDOFF_KIND}', got '{value}'"
            )
        return value

    @model_validator(mode="after")
    def _validate_payload(self) -> Self:
        expected = _KIND_PAYLOAD_MODELS.get(self.kind)
        if expected is not None and type(self.payload) is not expected:
            raise ValueError(
                f"payload for kind '{self.kind}' must be {expected.__name__}, "
                f"got {type(self.payload).__name__}"
            )
        return self


class ProposeTool(AgentTool[ProposeArgs]):
    name: ClassVar[str] = PROPOSE_TOOL_NAME
    description: ClassVar[str] = (
        "Propose a configuration change (such as data connections or project handoff) "
        "for user approval."
    )
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = ProposeArgs

    def __init__(self, proposals: ProposalsService, kinds: ProposalKinds) -> None:
        self._proposals = proposals
        self._kinds = kinds

    async def preview(self, ctx: ToolContext, args: ProposeArgs) -> Preview:
        try:
            kind_obj = self._kinds.get(args.kind)
            key = kind_obj.key(args.payload)
            await self._proposals.create(
                project_id=ctx.project_id,
                agent=ctx.agent,
                kind=args.kind,
                key=key,
                payload=args.payload.model_dump(),
                turn_id=ctx.turn_id,
                tool_call_id=ctx.tool_call_id,
            )
            kind_preview = kind_obj.preview(args.payload)
            return Preview(
                kind=kind_preview.kind,
                title=f"Proposal: {args.kind}",
                body=kind_preview.body,
            )
        except DomainError as exc:
            raise ToolFailure(str(exc)) from exc

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        proposal = await self._proposals.for_call(ctx.project_id, ctx.turn_id, ctx.tool_call_id)
        if proposal is None or proposal.result is None:
            return ToolOutput("Could not read proposal state for this call.", is_error=True)

        result = proposal.result
        if result.failed:
            failed_parts = ", ".join(f"{k}: {v}" for k, v in result.failed.items())
            if result.applied:
                applied_parts = ", ".join(result.applied)
                content = f"Approved. Applied: {applied_parts}. Failed: {failed_parts}."
            else:
                content = f"Approved. Failed: {failed_parts}."
            return ToolOutput(content=content, is_error=True)

        return ToolOutput(content="Approved. Everything was applied.", is_error=False)
