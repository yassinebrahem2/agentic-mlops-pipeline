"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.events.contract import EventLog
from mlops_agents.sandbox.containers import (
    ContainerManager,
    container_name,
    host_container_user,
)
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)
from mlops_agents.sandbox.docker import (
    DOCKER_INSTALL_MESSAGE,
    CliDockerRunner,
    DockerRunner,
    detect_docker,
)
from mlops_agents.sandbox.images import ImageManager, snapshot_image
from mlops_agents.sandbox.projects import SandboxProjects


class SandboxService(Sandbox):
    """Manages Docker sandbox environments and task execution."""

    def __init__(
        self,
        runner: DockerRunner | None = None,
        *,
        projects: SandboxProjects,
        events: EventLog,
    ) -> None:
        self._runner: DockerRunner = runner if runner is not None else CliDockerRunner()
        self._image_manager = ImageManager(self._runner)
        self._container_manager = ContainerManager(self._runner, user=host_container_user())
        self._projects = projects
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._task_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._status: dict[str, EnvironmentStatus] = {}

    def _get_task_lock(self, project_id: str, task_id: str) -> asyncio.Lock:
        key = (project_id, task_id)
        lock = self._task_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[key] = lock
        return lock

    async def docker(self) -> DockerStatus:
        return await detect_docker(self._runner)

    async def environment(self, project_id: str) -> EnvironmentStatus:
        current = self._status.get(project_id)
        if current is not None and current.state == "building":
            return current
        root = await self._projects.root(project_id)
        try:
            inputs = snapshot_image(project_id, root)
            if current is not None and current.state == "failed" and current.tag == inputs.tag:
                return current
            exists = await self._image_manager.image_exists(inputs.tag)
        except FileNotFoundError:
            return EnvironmentStatus(state="none", tag=None, detail=None)
        except (ValueError, OSError) as exc:
            status = EnvironmentStatus(state="failed", tag=None, detail=str(exc))
            self._status[project_id] = status
            return status
        status = EnvironmentStatus(
            state="ready" if exists else "none", tag=inputs.tag if exists else None, detail=None
        )
        self._status[project_id] = status
        return status

    async def _build_project_image(self, project_id: str, worktree: Path) -> EnvironmentStatus:
        """Own the full build lifecycle, including dependency reads and context creation."""
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            tag: str | None = None
            try:
                inputs = snapshot_image(project_id, worktree)
                tag = inputs.tag
                if await self._image_manager.image_exists(tag):
                    status = EnvironmentStatus(state="ready", tag=tag, detail=None)
                    self._status[project_id] = status
                    return status
                self._status[project_id] = EnvironmentStatus(state="building", tag=tag, detail=None)
                await self._events.record(
                    project_id,
                    source="system",
                    type="sandbox.build.started",
                    payload={"tag": tag, "detail": None},
                )
                status = await self._image_manager.build_image(project_id, worktree, inputs=inputs)
            except (OSError, ValueError) as exc:
                status = EnvironmentStatus(
                    state="failed", tag=tag, detail=f"Image build failed: {exc}"
                )
            self._status[project_id] = status
            await self._events.record(
                project_id,
                source="system",
                type="sandbox.build.finished"
                if status.state == "ready"
                else "sandbox.build.failed",
                payload={
                    "tag": status.tag,
                    "detail": None if status.state == "ready" else status.detail,
                },
            )
            return status

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        root = await self._projects.root(project_id)
        return await self._build_project_image(project_id, root)

    async def get_network(self, project_id: str) -> bool:
        return await self._projects.get_network(project_id)

    async def set_network(self, project_id: str, enabled: bool) -> bool:
        old = await self.get_network(project_id)
        await self._projects.set_network(project_id, enabled)
        if old != enabled:
            await self._container_manager.remove_project_containers(project_id)
        return enabled

    async def ensure_container(self, project_id: str, task: TaskRef) -> str:
        docker_status = await self.docker()
        if not docker_status.available:
            raise SandboxUnavailable(docker_status.detail or "Docker is not available.")

        lock = self._locks.setdefault(f"{project_id}:{task.id}", asyncio.Lock())
        async with lock:
            status = await self._build_project_image(project_id, task.worktree)
            if status.state != "ready" or not status.tag:
                raise SandboxUnavailable(
                    f"Environment build failed: {status.detail or 'unknown error'}"
                )
            tag = status.tag

            network = await self.get_network(project_id)
            try:
                return await self._container_manager.ensure_container(
                    project_id, task, tag, network=network
                )
            except FileNotFoundError as exc:
                raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from exc
            except OSError as exc:
                raise SandboxUnavailable(f"Sandbox container operation failed: {exc}") from exc

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        raise NotImplementedError("Run will be implemented in task 4.")

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None]:
        lock = self._get_task_lock(project_id, task_id)
        async with lock:
            cname = container_name(task_id)
            info = await self._container_manager.inspect(cname)
            if info is None or not info.running:
                yield
                return
            await self._container_manager.pause(cname)
            try:
                yield
            finally:
                await self._container_manager.unpause(cname)

    async def remove_task(self, project_id: str, task_id: str) -> None:
        lock = self._get_task_lock(project_id, task_id)
        async with lock:
            await self._container_manager.remove_task(project_id, task_id)
            self._task_locks.pop((project_id, task_id), None)

    async def close(self) -> None:
        await self._container_manager.cleanup_all()

    async def cleanup_orphans(self) -> None:
        docker_status = await self.docker()
        if docker_status.available:
            await self._container_manager.cleanup_orphans()
