"""ScheduleWakeup tool (Task 4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.messaging.wakeups import (
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    WakeupLimitReached,
    WakeupPermissionError,
)
from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.agents.catalog import AgentCatalog
    from mlops_agents.agents.messaging.wakeups import WakeupService


class ScheduleWakeupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_seconds: int = Field(
        ge=MIN_DELAY_SECONDS,
        le=MAX_DELAY_SECONDS,
        description=f"Seconds until the wake-up fires (between {MIN_DELAY_SECONDS} and {MAX_DELAY_SECONDS}).",
    )
    note: str = Field(
        min_length=1,
        description="What you need to be reminded of when you wake up.",
    )


class ScheduleWakeupTool(AgentTool[ScheduleWakeupArgs]):
    name: ClassVar[str] = "ScheduleWakeup"
    description: ClassVar[str] = (
        "Schedule a wake-up message to be delivered to your session after a delay."
    )
    risk: ClassVar[RiskClass] = RiskClass.MESSAGE
    args_model: ClassVar[type[BaseModel]] = ScheduleWakeupArgs

    def __init__(
        self,
        wakeups: WakeupService,
        catalog: AgentCatalog | None = None,
    ) -> None:
        self._wakeups = wakeups
        self._catalog = catalog

    async def run(self, ctx: ToolContext, args: ScheduleWakeupArgs) -> ToolOutput:
        catalog = self._catalog or self._wakeups.catalog
        if catalog is not None:
            try:
                caller = catalog.get(ctx.agent)
            except DomainError as exc:
                raise ToolFailure(str(exc)) from None
            if not caller.schedules:
                raise ToolFailure(f"Agent '{ctx.agent}' is not allowed to schedule wake-ups.")

        try:
            await self._wakeups.schedule(
                ctx.project_id,
                session_id=ctx.session_id,
                agent=ctx.agent,
                delay_seconds=args.delay_seconds,
                note=args.note,
            )
        except (WakeupLimitReached, WakeupPermissionError, DomainError, ValueError) as exc:
            raise ToolFailure(str(exc)) from None

        return ToolOutput(f"Scheduled wake-up in {args.delay_seconds} seconds.")
