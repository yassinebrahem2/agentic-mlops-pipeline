"""Sandbox service implementation."""

from __future__ import annotations

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
)
from mlops_agents.sandbox.docker import CliDockerRunner, DockerRunner, detect_docker


class SandboxService(Sandbox):
    """Manages Docker sandbox environments and task execution."""

    def __init__(self, runner: DockerRunner | None = None) -> None:
        self._runner = runner if runner is not None else CliDockerRunner()

    async def docker(self) -> DockerStatus:
        return await detect_docker(self._runner)

    async def environment(self, project_id: str) -> EnvironmentStatus:
        return EnvironmentStatus(state="none", tag=None, detail=None)

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        raise NotImplementedError("Rebuild will be implemented in task 2.")

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        raise NotImplementedError("Run will be implemented in task 4.")

    async def remove_task(self, project_id: str, task_id: str) -> None:
        pass

    async def close(self) -> None:
        pass
