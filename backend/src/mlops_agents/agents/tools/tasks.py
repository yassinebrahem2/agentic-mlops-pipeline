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
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks


class StartTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Short title describing the task to work on.")


class StartTaskTool(AgentTool[StartTaskArgs]):
    name: ClassVar[str] = "StartTask"
    description: ClassVar[str] = "Create a new task workspace for this agent and make it current."
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = StartTaskArgs

    def __init__(self, tasks: Tasks) -> None:
        self._tasks = tasks

    async def run(self, ctx: ToolContext, args: StartTaskArgs) -> ToolOutput:
        current = ctx.task or await self._tasks.current(ctx.project_id, ctx.session_id)
        if current is not None and await self._tasks.has_unpublished_changes(
            ctx.project_id, current
        ):
            raise ToolFailure(
                f"Current task {current.id} ({current.title}) has unpublished changes. "
                "Commit or discard them before starting a new task."
            )
        try:
            task = await self._tasks.start(ctx.project_id, ctx.session_id, ctx.agent, args.title)
        except DomainError as exc:
            raise ToolFailure(exc.message) from None
        return ToolOutput(
            f"Task {task.id} ({task.title})\nBranch: {task.branch}\nWorktree: {task.worktree}"
        )


class ResumeTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(description="ID of the task to resume.")


class ResumeTaskTool(AgentTool[ResumeTaskArgs]):
    name: ClassVar[str] = "ResumeTask"
    description: ClassVar[str] = (
        "Resume an earlier active or in-review task of this agent and make it current."
    )
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = ResumeTaskArgs

    def __init__(self, tasks: Tasks) -> None:
        self._tasks = tasks

    async def run(self, ctx: ToolContext, args: ResumeTaskArgs) -> ToolOutput:
        try:
            task = await self._tasks.resume(ctx.project_id, ctx.session_id, ctx.agent, args.task_id)
        except DomainError as exc:
            raise ToolFailure(exc.message) from None
        return ToolOutput(
            f"Task {task.id} ({task.title})\nBranch: {task.branch}\nWorktree: {task.worktree}"
        )
