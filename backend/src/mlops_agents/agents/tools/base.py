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
    pending_note: str | None = None


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


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema.pop("title", None)
    props = schema.get("properties")
    if isinstance(props, dict):
        for prop_val in cast(dict[str, Any], props).values():
            if isinstance(prop_val, dict):
                prop_dict = cast(dict[str, Any], prop_val)
                prop_dict.pop("title", None)
                if "default" in prop_dict and prop_dict["default"] is None:
                    any_of = cast(list[object] | None, prop_dict.get("anyOf"))
                    if isinstance(any_of, list) and len(any_of) == 2:
                        first = (
                            cast(dict[str, Any], any_of[0]) if isinstance(any_of[0], dict) else None
                        )
                        second = (
                            cast(dict[str, Any], any_of[1]) if isinstance(any_of[1], dict) else None
                        )
                        unwrapped: dict[str, Any] | None = None
                        if first == {"type": "null"} and second is not None:
                            unwrapped = dict(second)
                        elif second == {"type": "null"} and first is not None:
                            unwrapped = dict(first)
                        if unwrapped is not None:
                            del prop_dict["anyOf"]
                            unwrapped.pop("title", None)
                            for k, v in unwrapped.items():
                                if k not in prop_dict:
                                    prop_dict[k] = v
                _clean_schema(prop_dict)
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for d in cast(dict[str, Any], defs).values():
            if isinstance(d, dict):
                _clean_schema(cast(dict[str, Any], d))
    items = schema.get("items")
    if isinstance(items, dict):
        cast(dict[str, Any], items).pop("title", None)
        _clean_schema(cast(dict[str, Any], items))
    return schema


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
                "parameters": _clean_schema(self.args_model.model_json_schema(by_alias=True)),
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
