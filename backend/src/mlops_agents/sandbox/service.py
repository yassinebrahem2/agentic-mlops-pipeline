"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.events.contract import EventLog
from mlops_agents.sandbox.containers import ContainerManager, host_container_user
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)
from mlops_agents.sandbox.docker import CliDockerRunner, DockerRunner, detect_docker
from mlops_agents.sandbox.images import ImageManager, snapshot_image
from mlops_agents.sandbox.projects import SandboxProjects

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
        projects: SandboxProjects,
        events: EventLog | None = None,
    ) -> None:
        self._runner: DockerRunner = runner if runner is not None else CliDockerRunner()
        self._image_manager = ImageManager(self._runner)
        self._container_manager = ContainerManager(self._runner, user=host_container_user())
        self._projects = projects
        self._events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._status: dict[str, EnvironmentStatus] = {}

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
            started = time.monotonic()
            try:
                inputs = snapshot_image(project_id, worktree)
                tag = inputs.tag
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
                        payload={"tag": tag, "template": inputs.template_name},
                    )
                status = await self._image_manager.build_image(project_id, worktree, inputs=inputs)
            except (OSError, ValueError) as exc:
                status = EnvironmentStatus(
                    state="failed", tag=tag, detail=f"Image build failed: {exc}"
                )
            self._status[project_id] = status
            if self._events is not None:
                await self._events.record(
                    project_id,
                    source="system",
                    type="sandbox.build.finished"
                    if status.state == "ready"
                    else "sandbox.build.failed",
                    payload=(
                        {"tag": status.tag, "duration_s": round(time.monotonic() - started, 2)}
                        if status.state == "ready"
                        else {"tag": status.tag, "detail": status.detail}
                    ),
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
        except TimeoutError as exc:
            with contextlib.suppress(Exception):
                await self._runner.run(["restart", "-t", "0", cname], timeout_s=30.0)
            captured = ""
            stdout_val = getattr(exc, "stdout", "")
            stderr_val = getattr(exc, "stderr", "")
            if isinstance(stdout_val, str) and isinstance(stderr_val, str):
                captured = (stdout_val + stderr_val).strip()
            msg = f"Command took longer than {timeout} seconds and was stopped."
            if captured:
                out, trunc = truncate_output(captured)
                output = f"{out}\n\n{msg}"
            else:
                output = msg
                trunc = False
            return CommandResult(
                output=output,
                exit_code=None,
                timed_out=True,
                truncated=trunc,
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
        docker_status = await self.docker()
        if docker_status.available:
            await self._container_manager.cleanup_orphans()
