"""SaveReport tool (Task 2, #496)."""

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
from mlops_agents.agents.tools.names import SAVE_REPORT
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.promotion.contract import ReportDraft

if TYPE_CHECKING:
    from mlops_agents.promotion.contract import Reports


class SaveReportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: ReportDraft = Field(
        description="The report draft containing candidate model, version, and required assessment fields."
    )


class SaveReport(AgentTool[SaveReportArgs]):
    name: ClassVar[str] = SAVE_REPORT
    description: ClassVar[str] = (
        "Save a retraining evaluation report with required assessment fields. "
        "Returns the report ID, the candidate's evaluation decision and reasons, "
        "and any uncomputed evaluation fields."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = SaveReportArgs

    def __init__(self, reports: Reports) -> None:
        self._reports = reports

    async def action_call(self, args: SaveReportArgs, ctx: ToolContext) -> None:
        return None

    async def run(self, ctx: ToolContext, args: SaveReportArgs) -> ToolOutput:
        try:
            report = await self._reports.save(ctx.project_id, args.draft, author=ctx.agent)
        except DomainError as exc:
            raise ToolFailure(exc.message) from exc

        parts = [
            f"Saved report {report.id} for model {report.draft.model} version {report.draft.candidate_version}."
        ]

        if report.evaluation is not None:
            ev = report.evaluation
            parts.append("Evaluation:")
            if ev.payload.decision is not None:
                parts.append(f"Decision: {untrusted(ev.payload.decision, source='evaluation')}")
            else:
                parts.append("Decision: None")

            if ev.payload.reasons is None:
                parts.append("Reasons: not computed")
            elif ev.payload.reasons:
                parts.append("Reasons:")
                for r in ev.payload.reasons:
                    parts.append(untrusted(r, source="evaluation"))
            else:
                parts.append("Reasons: None")

            if ev.not_computed:
                parts.append("Not computed fields:")
                for name, pr in sorted(ev.not_computed.items()):
                    pr_note = f" (open PR #{pr})" if pr is not None else ""
                    parts.append(f"- {name}{pr_note}")
        else:
            parts.append("No evaluation record found for this candidate.")

        return ToolOutput("\n".join(parts))
