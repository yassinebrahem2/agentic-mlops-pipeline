"""ProposalShape tool: inspect a proposal kind's payload schema (Plan #712)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.names import PROPOSAL_SHAPE
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.proposals.kinds import ProposalKinds

PROPOSAL_SHAPE_TOOL_NAME: Final = PROPOSAL_SHAPE


class ProposalShapeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="The proposal kind to get the payload schema for.")


class ProposalShapeTool(AgentTool[ProposalShapeArgs]):
    name: ClassVar[str] = PROPOSAL_SHAPE
    description: ClassVar[str] = (
        "Return the JSON schema for a proposal kind's payload. "
        "Call this before calling Propose to inspect the expected fields and rules."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ProposalShapeArgs

    def __init__(self, kinds: ProposalKinds) -> None:
        self._kinds = kinds

    def shape_for(self, kind: str) -> dict[str, Any]:
        """Return the JSON schema dict of the given proposal kind."""
        kind_obj = self._kinds.get(kind)
        return kind_obj.payload_model.model_json_schema()

    def __call__(self, kind: str) -> dict[str, Any]:
        """Convenience helper returning the raw schema dict."""
        return self.shape_for(kind)

    async def run(self, ctx: ToolContext, args: ProposalShapeArgs) -> ToolOutput:
        try:
            kind_obj = self._kinds.get(args.kind)
        except DomainError:
            available = ", ".join(self._kinds.names())
            return ToolOutput(
                f"Unknown proposal kind: {args.kind!r}. Available kinds: {available}.",
                is_error=True,
            )

        schema = kind_obj.payload_model.model_json_schema()
        return ToolOutput(content=json.dumps(schema, indent=2), is_error=False)


ProposalShape = ProposalShapeTool
