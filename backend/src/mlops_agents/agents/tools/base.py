from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, cast

from pydantic import BaseModel, ValidationError


class RiskClass(StrEnum):
    """Tool risk classes from docs/roadmap.md. The permission policy (stage 2) keys on these."""

    READ = "read"
    WRITE_CODE = "write_code"
    RUN = "run"
    MESSAGE = "message"
    GITHUB = "github"
    DEPLOY = "deploy"


class ToolFailure(Exception):
    """An expected failure the agent should read and react to, such as a missing file."""


@dataclass(frozen=True)
class ToolOutput:
    content: str
    is_error: bool = False


class FileAccess(Protocol):
    @property
    def root(self) -> Path: ...

    async def tracked(self) -> list[str]: ...

    async def under(self, prefix: str) -> list[str]: ...

    async def resolve_file(self, user_path: str) -> str: ...

    async def resolve_dir(self, user_path: str | None) -> str: ...

    def invalidate(self) -> None:
        """Drop the cached listing after a change."""
        ...


TaskStatus = Literal["active", "in_review", "merged", "closed", "discarded"]


@dataclass(frozen=True)
class TaskRef:
    id: str
    title: str
    branch: str
    worktree: Path
    status: TaskStatus


@dataclass(frozen=True)
class ToolContext:
    """Built for every tool call."""

    project_id: str
    agent: str
    session_id: str
    turn_id: str
    files: FileAccess  # the current task's worktree view, else the repository's
    task: TaskRef | None


class Preview(BaseModel):
    kind: Literal["diff", "command", "text"]
    title: str
    body: str


def _summary(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )


class AgentTool[A: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[RiskClass]
    args_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, ctx: ToolContext, args: A) -> ToolOutput: ...

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(by_alias=True),
            },
        }

    def parse(self, raw_args: Mapping[str, Any]) -> A | ToolOutput:
        """The validated arguments, or the tool error to give the model when they are invalid."""
        try:
            return cast(A, self.args_model.model_validate(dict(raw_args)))
        except ValidationError as exc:
            return ToolOutput(f"Invalid arguments for {self.name}: {_summary(exc)}", is_error=True)

    async def preview(self, ctx: ToolContext, args: A) -> Preview:
        return Preview(
            kind="text", title=self.name, body=args.model_dump_json(indent=2, by_alias=True)
        )

    async def invoke(self, ctx: ToolContext, raw_args: Mapping[str, Any]) -> ToolOutput:
        args = self.parse(raw_args)
        if isinstance(args, ToolOutput):
            return args
        try:
            return await self.run(ctx, args)
        except ToolFailure as exc:
            return ToolOutput(str(exc), is_error=True)


class ToolRegistry:
    def __init__(self, tools: Iterable[AgentTool[Any]] = ()) -> None:
        self._tools: dict[str, AgentTool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} is registered twice")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool[Any] | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def schemas(self, names: Iterable[str]) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in names]
