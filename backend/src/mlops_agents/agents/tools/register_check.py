"""RegisterCheck tool (I33)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.names import REGISTER_CHECK
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.results.mlflow_source import handoff_metric_user
from mlops_agents.proposals.payloads import Cadence, FileResults, MlflowResults

if TYPE_CHECKING:
    from mlops_agents.monitoring.expectations.checks import ChecksStore
    from mlops_agents.proposals.contract import Handoffs


class RegisterCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(description="Check identifier (e.g. model_drift)")
    category: ResultCategory = Field(
        description="Result category: data_quality, model_performance, or operational"
    )
    cadence: Cadence = Field(description="Cadence specification (cron or every_minutes)")
    timezone: str = Field(description="IANA timezone (e.g. UTC, Europe/Paris)")
    results_location: FileResults | MlflowResults = Field(
        description="Results location (file or mlflow)"
    )
    pr_number: int = Field(description="The PR number this check is being introduced in")


class RegisterCheck(AgentTool[RegisterCheckInput]):
    name: ClassVar[str] = REGISTER_CHECK
    description: ClassVar[str] = "Register a monitoring check pending a pull request merge."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = RegisterCheckInput

    def __init__(self, checks: ChecksStore, handoffs: Handoffs) -> None:
        self._checks = checks
        self._handoffs = handoffs

    async def _metric_user(self, project_id: str, check_id: str, metric: str) -> str | None:
        """Who else already reads this MLflow metric, so two checks never share one source.

        The same check registering its metric again is not a conflict, even when the handoff
        names that metric too: the registered check already wins it in the MLflow source.
        """
        others: list[str] = []
        for chk in await self._checks.list(project_id):
            location = chk.results_location
            if (
                chk.status == "retired"
                or not isinstance(location, MlflowResults)
                or location.metric != metric
            ):
                continue
            if chk.check_id == check_id:
                return None
            others.append(f"registered check {chk.check_id!r}")

        user = handoff_metric_user(await self._handoffs.current(project_id), metric)
        if user is not None:
            if user.check is not None:
                return f"handoff check {user.check.check_id!r}"
            return "the handoff's results location"
        return others[0] if others else None

    async def run(self, ctx: ToolContext, args: RegisterCheckInput) -> ToolOutput:
        # Validate timezone
        try:
            ZoneInfo(args.timezone)
        except Exception as exc:
            return ToolOutput(content=f"Malformed timezone {args.timezone!r}: {exc}", is_error=True)

        # Validate cadence
        if args.cadence.cron is not None and not croniter.is_valid(args.cadence.cron):
            return ToolOutput(
                content=f"Malformed cron expression in cadence: {args.cadence.cron!r}",
                is_error=True,
            )

        # Validate results_location
        if isinstance(args.results_location, FileResults):
            if not args.results_location.connection or not args.results_location.path_pattern:
                return ToolOutput(
                    content="Malformed file results location: connection and path_pattern are required.",
                    is_error=True,
                )
        else:
            if not args.results_location.metric:
                return ToolOutput(
                    content="Malformed mlflow results location: metric is required.",
                    is_error=True,
                )

        if isinstance(args.results_location, MlflowResults):
            try:
                user = await self._metric_user(
                    ctx.project_id, args.check_id, args.results_location.metric
                )
            except Exception as exc:
                return ToolOutput(
                    content=f"Failed to check the metric's users: {exc}", is_error=True
                )
            if user is not None:
                return ToolOutput(
                    content=(
                        f"MLflow metric {args.results_location.metric!r} is already used by "
                        f"{user}; one metric feeds one check."
                    ),
                    is_error=True,
                )

        try:
            view = await self._checks.register(
                ctx.project_id,
                check_id=args.check_id,
                category=args.category,
                cadence=args.cadence,
                timezone=args.timezone,
                results_location=args.results_location,
                pr_number=args.pr_number,
                agent=ctx.agent,
            )
        except Exception as exc:
            return ToolOutput(content=f"Failed to register check: {exc}", is_error=True)

        return ToolOutput(
            content=f"Registered check {view.check_id} (ID: {view.id}) with status {view.status} pending PR #{view.pr_number}."
        )
