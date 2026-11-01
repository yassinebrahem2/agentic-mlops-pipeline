"""The Propose tool (I19): lets agents propose configuration changes for user approval."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self, cast

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError, validation_error_message
from mlops_agents.proposals.contract import ProposalCheck

if TYPE_CHECKING:
    from mlops_agents.proposals.kinds import ProposalKinds
    from mlops_agents.proposals.service import ProposalsService

PROPOSE_TOOL_NAME: Final = "Propose"


class ProposeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    payload: Any

    @model_validator(mode="after")
    def _parse_known_payload(self) -> Self:
        from mlops_agents.proposals.payloads import ConnectionsPayload, HandoffPayload

        payload_obj: object = self.payload
        if isinstance(payload_obj, dict):
            dict_payload = cast(dict[str, Any], payload_obj)
            if self.kind == "connections":
                with suppress(ValidationError):
                    self.payload = ConnectionsPayload.model_validate(dict_payload)
            elif self.kind == "handoff":
                with suppress(ValidationError):
                    self.payload = HandoffPayload.model_validate(dict_payload)
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

    def schema(self) -> dict[str, Any]:
        """The tool as the model sees it, with the script contract a `parser` proposal's code
        is written against (the same text its approval card shows)."""
        # parser_kind imports this package (for Preview), so its contract is read here, at call
        # time, not when this module loads.
        from mlops_agents.monitoring.results.contract import ResultRecord
        from mlops_agents.monitoring.results.parser_kind import CODE_PARSER_CONTRACT

        ran_at_desc = ResultRecord.model_fields["ran_at"].description
        out = super().schema()
        out["function"]["description"] = (
            f"{self.description}\n\n"
            f"A `parser` proposal with `code` runs its script under this contract:\n"
            f"{CODE_PARSER_CONTRACT}\n\n"
            f"Records need `ran_at`: {ran_at_desc}"
        )
        return out

    async def preview(self, ctx: ToolContext, args: ProposeArgs) -> Preview:
        try:
            kind_obj = self._kinds.get(args.kind)
        except DomainError as exc:
            raise ToolFailure(f"Unknown proposal kind: {args.kind}") from exc

        try:
            payload = (
                args.payload
                if isinstance(args.payload, kind_obj.payload_model)
                else kind_obj.payload_model.model_validate(args.payload)
            )
        except ValidationError as exc:
            raise ToolFailure(
                f"Invalid payload for {args.kind}: {validation_error_message(exc).text}"
            ) from exc

        if isinstance(kind_obj, ProposalCheck):
            refusals = await kind_obj.check(ctx.project_id, payload)
            if refusals:
                raise ToolFailure("\n".join(refusals))

        key = kind_obj.key(payload)
        payload_dump = payload.model_dump()
        try:
            await self._proposals.create(
                project_id=ctx.project_id,
                agent=ctx.agent,
                kind=args.kind,
                key=key,
                payload=payload_dump,
                turn_id=ctx.turn_id,
                tool_call_id=ctx.tool_call_id,
            )
        except DomainError as exc:
            raise ToolFailure(str(exc)) from exc

        kind_preview = kind_obj.preview(payload)
        return Preview(
            kind=kind_preview.kind,
            title=f"Proposal: {args.kind}",
            body=kind_preview.body,
        )

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        proposal = await self._proposals.for_call(ctx.project_id, ctx.turn_id, ctx.tool_call_id)
        if proposal is None:
            return ToolOutput("Could not read proposal state for this call.", is_error=True)

        if proposal.result is None:
            result = await self._proposals.apply_allowed(ctx.project_id, proposal.id)
        else:
            result = proposal.result

        if result is None:
            return ToolOutput("Could not read proposal state for this call.", is_error=True)

        if result.failed:
            failed_parts = ", ".join(f"{k}: {v}" for k, v in result.failed.items())
            if result.applied:
                applied_parts = ", ".join(result.applied)
                content = f"Approved. Applied: {applied_parts}. Failed: {failed_parts}."
            else:
                content = f"Approved. Failed: {failed_parts}."
            return ToolOutput(content=content, is_error=True)

        return ToolOutput(content="Approved. Everything was applied.", is_error=False)
