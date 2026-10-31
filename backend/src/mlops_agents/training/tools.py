"""RequestFix and RecordFixChoice tools (Task 1, #486)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.agents.tools.names import RECORD_FIX_CHOICE, REQUEST_FIX
from mlops_agents.errors import DomainError
from mlops_agents.training.contract import FixChoice

if TYPE_CHECKING:
    from mlops_agents.training.fixes import SqliteFixes


class RequestFixArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(description="Incident ID requesting a model fix.")
    evidence: str = Field(description="Evidence supporting the fix request.")


class RequestFix(AgentTool[RequestFixArgs]):
    name: ClassVar[str] = REQUEST_FIX
    description: ClassVar[str] = "Request a model fix for an incident. Wakes the Retraining agent."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = RequestFixArgs

    def __init__(self, fixes: SqliteFixes) -> None:
        self._fixes = fixes

    async def action_call(self, args: RequestFixArgs, ctx: ToolContext) -> None:
        return None

    async def run(self, ctx: ToolContext, args: RequestFixArgs) -> ToolOutput:
        try:
            fix = await self._fixes.request(
                ctx.project_id,
                args.incident_id,
                ctx.agent,
                args.evidence,
            )
        except DomainError as exc:
            raise ToolFailure(exc.message) from exc

        return ToolOutput(f"Fix {fix.id} is {fix.status}.")


class RecordFixChoiceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fix_id: str = Field(description="Fix request ID.")
    choice: FixChoice = Field(
        description="The chosen fix action ('re_threshold', 'recalibrate', 'retrain', or 'decline')."
    )
    why: str = Field(description="Rationale for the choice.")
    why_not: dict[str, str] = Field(
        default_factory=dict,
        description="Reasons for not choosing each of the other fix options.",
    )


class RecordFixChoice(AgentTool[RecordFixChoiceArgs]):
    name: ClassVar[str] = RECORD_FIX_CHOICE
    description: ClassVar[str] = (
        "Record the fix choice for a requested fix. Only Retraining can choose."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = RecordFixChoiceArgs

    def __init__(self, fixes: SqliteFixes) -> None:
        self._fixes = fixes

    async def action_call(self, args: RecordFixChoiceArgs, ctx: ToolContext) -> None:
        return None

    async def run(self, ctx: ToolContext, args: RecordFixChoiceArgs) -> ToolOutput:
        try:
            fix = await self._fixes.choose(
                ctx.project_id,
                args.fix_id,
                ctx.agent,
                args.choice,
                args.why,
                args.why_not,
            )
        except DomainError as exc:
            raise ToolFailure(exc.message) from exc

        return ToolOutput(f"Fix {fix.id} is {fix.status}.")
