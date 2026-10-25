"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.sandbox.containers import ContainerManager
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)
from mlops_agents.sandbox.docker import CliDockerRunner, DockerRunner, detect_docker
from mlops_agents.sandbox.environment import detect_environment
from mlops_agents.sandbox.images import ImageManager
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.workspace.service import WorkspaceService

MAX_CAPTURE_CHARS = 30_000
HALF_CAPTURE_CHARS = 15_000


def truncate_output(raw_output: str) -> tuple[str, bool]:
    if len(raw_output) <= MAX_CAPTURE_CHARS:
        return raw_output, False
    head = raw_output[:HALF_CAPTURE_CHARS]
    tail = raw_output[-HALF_CAPTURE_CHARS:]
    cut_count = len(raw_output) - MAX_CAPTURE_CHARS
    cut_note = f"\n\n[... {cut_count} characters truncated ...]\n\n"
    return f"{head}{cut_note}{tail}", True


class SandboxService(Sandbox):
    """Manages Docker sandbox environments and task execution."""

    def __init__(
        self,
        runner: DockerRunner | None = None,
        *,
        databases: Databases | None = None,
        workspace: WorkspaceService | None = None,
        events: EventLog | None = None,
    ) -> None:
        self._docker_runner: DockerRunner = runner if runner is not None else CliDockerRunner()
        self._image_manager = ImageManager(self._docker_runner)
        self._container_manager = ContainerManager(self._docker_runner)
        self._databases = databases
        self._workspace = workspace
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._status: dict[str, EnvironmentStatus] = {}
        self._repo_roots: dict[str, Path] = {}
        self._network_settings: dict[str, bool] = {}

    @property
    def _runner(self) -> DockerRunner:
        return self._docker_runner

    @_runner.setter
    def _runner(self, runner: DockerRunner) -> None:
        self._docker_runner = runner
        self._image_manager = ImageManager(runner)
        self._container_manager = ContainerManager(runner)

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

    async def get_network(self, project_id: str) -> bool:
        if self._databases is not None:
            proj_engine = await self._databases.project(project_id)
            async with proj_engine.connect() as conn:
                row = (
                    await conn.execute(
                        select(project_settings.c.value).where(
                            project_settings.c.key == "sandbox_network"
                        )
                    )
                ).scalar_one_or_none()
            if row is not None:
                return row.strip().lower() == "true"
            return False
        return self._network_settings.get(project_id, False)

    async def set_network(self, project_id: str, enabled: bool) -> bool:
        old = await self.get_network(project_id)
        if self._databases is not None:
            proj_engine = await self._databases.project(project_id)
            stmt = (
                sqlite_insert(project_settings)
                .values(key="sandbox_network", value="true" if enabled else "false")
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": "true" if enabled else "false"},
                )
            )
            async with proj_engine.begin() as conn:
                await conn.execute(stmt)
        else:
            self._network_settings[project_id] = enabled

        if old != enabled:
            await self._container_manager.remove_project_containers(project_id)

        return enabled

    async def ensure_container(self, project_id: str, task: TaskRef) -> str:
        docker_status = await self.docker()
        if not docker_status.available:
            raise SandboxUnavailable(docker_status.detail or "Docker is not available.")

        lock = self._locks.setdefault(f"{project_id}:{task.id}", asyncio.Lock())
        async with lock:
            detected = detect_environment(task.worktree)
            tag = self._image_manager.tag_for(project_id, task.worktree, detected)
            if not await self._image_manager.image_exists(tag):
                proj_lock = self._locks.setdefault(project_id, asyncio.Lock())
                async with proj_lock:
                    if not await self._image_manager.image_exists(tag):
                        self._status[project_id] = EnvironmentStatus(
                            state="building", tag=tag, detail=None
                        )
                        if self._events is not None:
                            await self._events.record(
                                project_id,
                                source="system",
                                type="sandbox.build.started",
                                payload={"tag": tag, "template": detected.template},
                            )
                        start_time = time.monotonic()
                        status = await self._image_manager.build_image(
                            project_id, task.worktree, detected
                        )
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

                        if status.state != "ready" or not status.tag:
                            raise SandboxUnavailable(
                                f"Environment build failed: {status.detail or 'unknown error'}"
                            )
                        tag = status.tag

            network = await self.get_network(project_id)
            return await self._container_manager.ensure_container(
                project_id, task, tag, network=network
            )

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        cname = await self.ensure_container(project_id, task)
        timeout = max(1, min(timeout_s, 600)) if timeout_s > 0 else 120

        try:
            res = await self._runner.run(
                ["exec", cname, "bash", "-lc", command],
                timeout_s=timeout,
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._runner.run(["restart", "-t", "0", cname])
            return CommandResult(
                output=f"Command took longer than {timeout} seconds and was stopped.",
                exit_code=None,
                timed_out=True,
                truncated=False,
            )
        except FileNotFoundError as err:
            raise SandboxUnavailable("Docker is not installed or not on PATH.") from err

        raw_output = res.stdout + res.stderr
        output, truncated = truncate_output(raw_output)
        return CommandResult(
            output=output,
            exit_code=res.exit_code,
            timed_out=False,
            truncated=truncated,
        )

    async def remove_task(self, project_id: str, task_id: str) -> None:
        await self._container_manager.remove_task(project_id, task_id)

    async def close(self) -> None:
        await self._container_manager.cleanup_all()

    async def cleanup_orphans(self) -> None:
        with contextlib.suppress(Exception):
            docker_status = await self.docker()
            if not docker_status.available:
                return
            await self._container_manager.cleanup_orphans()
