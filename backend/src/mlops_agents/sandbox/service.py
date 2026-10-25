"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
)
from mlops_agents.sandbox.docker import CliDockerRunner, DockerRunner, detect_docker
from mlops_agents.sandbox.environment import detect_environment
from mlops_agents.sandbox.images import ImageManager
from mlops_agents.workspace.service import WorkspaceService


class SandboxService(Sandbox):
    """Manages Docker sandbox environments and task execution."""

    def __init__(
        self,
        runner: DockerRunner | None = None,
        *,
        workspace: WorkspaceService | None = None,
        events: EventLog | None = None,
    ) -> None:
        self._docker_runner: DockerRunner = runner if runner is not None else CliDockerRunner()
        self._image_manager = ImageManager(self._docker_runner)
        self._workspace = workspace
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._status: dict[str, EnvironmentStatus] = {}
        self._repo_roots: dict[str, Path] = {}

    @property
    def _runner(self) -> DockerRunner:
        return self._docker_runner

    @_runner.setter
    def _runner(self, runner: DockerRunner) -> None:
        self._docker_runner = runner
        self._image_manager = ImageManager(runner)

    def register_project_root(self, project_id: str, path: Path) -> None:
        """Registers a project's root path directly (used in tests)."""
        self._repo_roots[project_id] = path

    async def _get_repo_root(self, project_id: str) -> Path:
        if project_id in self._repo_roots:
            return self._repo_roots[project_id]
        if self._workspace is not None:
            project = await self._workspace.get(project_id)
            return self._workspace.repo_root(project)
        raise DomainError("project_not_found", "This project does not exist.", status=404)

    async def docker(self) -> DockerStatus:
        return await detect_docker(self._runner)

    async def environment(self, project_id: str) -> EnvironmentStatus:
        if project_id in self._status:
            curr = self._status[project_id]
            if curr.state in ("building", "failed"):
                return curr

        with contextlib.suppress(Exception):
            root = await self._get_repo_root(project_id)
            detected = detect_environment(root)
            tag = self._image_manager.tag_for(project_id, root, detected)
            if await self._image_manager.image_exists(tag):
                status = EnvironmentStatus(state="ready", tag=tag, detail=None)
                self._status[project_id] = status
                return status

        return self._status.get(project_id, EnvironmentStatus(state="none", tag=None, detail=None))

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            root = await self._get_repo_root(project_id)
            detected = detect_environment(root)
            tag = self._image_manager.tag_for(project_id, root, detected)

            if await self._image_manager.image_exists(tag):
                status = EnvironmentStatus(state="ready", tag=tag, detail=None)
                self._status[project_id] = status
                return status

            self._status[project_id] = EnvironmentStatus(state="building", tag=tag, detail=None)
            if self._events is not None:
                await self._events.record(
                    project_id,
                    source="system",
                    type="sandbox.build.started",
                    payload={"tag": tag, "template": detected.template},
                )

            start_time = time.monotonic()
            status = await self._image_manager.build_image(project_id, root, detected)
            duration_s = round(time.monotonic() - start_time, 2)
            self._status[project_id] = status

            if self._events is not None:
                if status.state == "ready":
                    await self._events.record(
                        project_id,
                        source="system",
                        type="sandbox.build.finished",
                        payload={"tag": tag, "duration_s": duration_s},
                    )
                elif status.state == "failed":
                    await self._events.record(
                        project_id,
                        source="system",
                        type="sandbox.build.failed",
                        payload={"tag": tag, "detail": status.detail or ""},
                    )

            return status

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        raise NotImplementedError("Run will be implemented in task 4.")

    async def remove_task(self, project_id: str, task_id: str) -> None:
        pass

    async def close(self) -> None:
        pass
