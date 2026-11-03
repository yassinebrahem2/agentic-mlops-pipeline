"""The Propose tool (I19): lets agents propose configuration changes for user approval."""

from __future__ import annotations

from collections.abc import Mapping
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
    clean_schema,
    format_validation_error,
)
from mlops_agents.agents.tools.proposal_shape import (
    PROPOSAL_SHAPE_TOOL_NAME as PROPOSAL_SHAPE,
)
from mlops_agents.errors import DomainError, validation_error_message
from mlops_agents.proposals.contract import PARSER_KIND, ProposalCheck

if TYPE_CHECKING:
    from mlops_agents.proposals.kinds import ProposalKinds
    from mlops_agents.proposals.service import ProposalsService

PROPOSE_TOOL_NAME: Final = "Propose"


def propose_tool_name(kind: str) -> str:
    """The tool name for a proposal kind: Propose followed by PascalCase kind name."""
    pascal = "".join(part.capitalize() for part in kind.split("_"))
    return f"Propose{pascal}"


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
        f"for user approval. Call {PROPOSAL_SHAPE}(kind) first to inspect that kind's "
        "expected payload schema and rules."
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
        from mlops_agents.proposals.payloads import CheckSpec

        ran_at_desc = ResultRecord.model_fields["ran_at"].description
        cadence_desc = CheckSpec.model_fields["cadence"].description
        out = super().schema()
        out["function"]["description"] = (
            f"{self.description}\n\n"
            f"A `parser` proposal with `code` runs its script under this contract:\n"
            f"{CODE_PARSER_CONTRACT}\n\n"
            f"Records need `ran_at`: {ran_at_desc}\n\n"
            f"Team checks' `cadence`: {cadence_desc}"
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
                f"Invalid payload for {args.kind}: {validation_error_message(exc).text}. "
                f"Call {PROPOSAL_SHAPE}(kind={args.kind!r}) to inspect the schema."
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


class TypedProposeTool(AgentTool[Any]):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = BaseModel

    def __init__(self, kind: str, proposals: ProposalsService, kinds: ProposalKinds) -> None:
        self.kind = kind
        self._proposals = proposals
        self._kinds = kinds
        object.__setattr__(self, "name", propose_tool_name(kind))

    def _payload_description(self) -> str:
        try:
            kind_obj = self._kinds.get(self.kind)
            return (kind_obj.payload_model.__doc__ or "").strip()
        except DomainError:
            return f"Propose a {self.kind} configuration change for user approval."

    def __getattribute__(self, name: str) -> Any:
        if name == "description":
            return self._payload_description()
        return super().__getattribute__(name)

    def schema(self) -> dict[str, Any]:
        kind_obj = self._kinds.get(self.kind)
        raw_schema = kind_obj.payload_model.model_json_schema(by_alias=True)
        raw_schema.pop("description", None)
        parameters = clean_schema(raw_schema)
        desc = self._payload_description()
        if self.kind == PARSER_KIND:
            from mlops_agents.monitoring.results.contract import ResultRecord
            from mlops_agents.monitoring.results.parser_kind import CODE_PARSER_CONTRACT
            from mlops_agents.proposals.payloads import CheckSpec

            ran_at_desc = ResultRecord.model_fields["ran_at"].description
            cadence_desc = CheckSpec.model_fields["cadence"].description
            desc = (
                f"{desc}\n\n"
                f"A `parser` proposal with `code` runs its script under this contract:\n"
                f"{CODE_PARSER_CONTRACT}\n\n"
                f"Records need `ran_at`: {ran_at_desc}\n\n"
                f"Team checks' `cadence`: {cadence_desc}"
            )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": parameters,
            },
        }

    def parse(self, raw_args: Mapping[str, Any]) -> BaseModel | ToolOutput:
        args_val: object = raw_args
        if isinstance(args_val, str):
            return ToolOutput(
                f"Invalid arguments for {self.name}: text arrived where an object belongs",
                is_error=True,
            )
        try:
            raw_args_dict = dict(raw_args)
        except (TypeError, ValueError):
            return ToolOutput(
                f"Invalid arguments for {self.name}: expected an object of arguments",
                is_error=True,
            )

        try:
            kind_obj = self._kinds.get(self.kind)
        except DomainError:
            return ToolOutput(f"Unknown proposal kind: {self.kind}", is_error=True)

        try:
            return kind_obj.payload_model.model_validate(raw_args_dict)
        except ValidationError as exc:
            return ToolOutput(
                f"Invalid arguments for {self.name}: {format_validation_error(exc)}",
                is_error=True,
            )

    async def preview(self, ctx: ToolContext, args: Any) -> Preview:
        try:
            kind_obj = self._kinds.get(self.kind)
        except DomainError as exc:
            raise ToolFailure(f"Unknown proposal kind: {self.kind}") from exc

        try:
            payload = (
                args
                if isinstance(args, kind_obj.payload_model)
                else kind_obj.payload_model.model_validate(args)
            )
        except ValidationError as exc:
            raise ToolFailure(
                f"Invalid payload for {self.kind}: {validation_error_message(exc).text}"
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
                kind=self.kind,
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
            title=f"Proposal: {self.kind}",
            body=kind_preview.body,
        )

    async def run(self, ctx: ToolContext, args: Any) -> ToolOutput:
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
