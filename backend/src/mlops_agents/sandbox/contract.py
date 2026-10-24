"""The Docker sandbox (I12)."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

from mlops_agents.agents.tools.base import TaskRef


class SandboxUnavailable(Exception):
    """Its message is written for the model and the user."""


class DockerStatus(BaseModel):
    available: bool
    version: str | None
    detail: str | None


class EnvironmentStatus(BaseModel):
    state: Literal["none", "building", "ready", "failed"]
    tag: str | None
    detail: str | None


class CommandResult(BaseModel):
    output: str
    exit_code: int | None  # None when timed out
    timed_out: bool
    truncated: bool


class Sandbox(Protocol):
    async def docker(self) -> DockerStatus: ...

    async def environment(self, project_id: str) -> EnvironmentStatus: ...

    async def rebuild(self, project_id: str) -> EnvironmentStatus: ...

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        """Raises SandboxUnavailable."""
        ...

    async def remove_task(self, project_id: str, task_id: str) -> None: ...

    async def close(self) -> None: ...
