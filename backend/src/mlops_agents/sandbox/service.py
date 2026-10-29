"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import NoReturn

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.events.contract import EventLog
from mlops_agents.sandbox.containers import (
    ContainerManager,
    container_name,
    host_container_user,
)
from mlops_agents.sandbox.contract import (
    DEFAULT_BASH_TIMEOUT_S,
    MAX_BASH_TIMEOUT_S,
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)
from mlops_agents.sandbox.docker import (
    CliDockerRunner,
    DockerRunner,
    DockerTimeoutError,
    detect_docker,
    docker_error_boundary,
)
from mlops_agents.sandbox.images import ImageManager, build_failure_detail, snapshot_image
from mlops_agents.sandbox.projects import SandboxProjects

logger = logging.getLogger(__name__)


async def _finish_operation[T](operation: Coroutine[object, object, T]) -> T:
    """Settle a Docker state change before cancellation can release the task lock."""
    pending = asyncio.create_task(operation)
    cancelled: asyncio.CancelledError | None = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = pending.result()
    if cancelled is not None:
        raise cancelled
    return result


def _raise_with_cleanup(body_error: BaseException, cleanup_error: BaseException) -> NoReturn:
    """Re-raise body_error while preserving cleanup_error details via add_note and chaining."""
    body_error.add_note(f"Cleanup failed: {cleanup_error}")
    if body_error.__cause__ is None:
        raise body_error from cleanup_error
    raise body_error


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
        self._build_locks: dict[str, asyncio.Lock] = {}
        self._task_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._task_owners: dict[tuple[str, str], asyncio.Task[object] | None] = {}
        self._task_entry = asyncio.Lock()
        self._status: dict[str, EnvironmentStatus] = {}

    @asynccontextmanager
    async def _task_guard(self, project_id: str, task_id: str) -> AsyncGenerator[None]:
        key = (project_id, task_id)
        owner = asyncio.current_task()
        self._reject_nested_operation()
        # Never evict: queued callers must share this lock with future callers after removal.
        async with self._task_entry:
            lock = self._task_locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._task_owners[key] = owner
            try:
                yield
            finally:
                del self._task_owners[key]

    def _reject_nested_operation(self) -> None:
        # Check before the entry gate: cleanup may hold it while waiting for this caller's lock.
        if asyncio.current_task() in self._task_owners.values():
            raise SandboxUnavailable(
                "Call sandbox operations after leaving paused() or another task operation."
            )

    @asynccontextmanager
    async def _exclusive_tasks(self, project_id: str | None = None) -> AsyncGenerator[None]:
        """Block new task operations while draining the same locks used by active operations."""
        self._reject_nested_operation()
        async with self._task_entry, AsyncExitStack() as held:
            for key, lock in self._task_locks.items():
                if project_id is None or key[0] == project_id:
                    await held.enter_async_context(lock)
            yield

    async def docker(self) -> DockerStatus:
        return await detect_docker(self._runner)

    async def environment(self, project_id: str) -> EnvironmentStatus:
        current = self._status.get(project_id)
        if current is not None and current.state == "building":
            return current
        docker_status = await self.docker()
        if not docker_status.available:
            return EnvironmentStatus(state="unavailable", tag=None, detail=docker_status.detail)
        root = await self._projects.root(project_id)
        try:
            inputs = snapshot_image(project_id, root)
            if current is not None and current.state == "failed" and current.tag == inputs.tag:
                return current
            exists = await self._image_manager.image_exists(inputs.tag)
        except SandboxUnavailable as exc:
            return EnvironmentStatus(state="unavailable", tag=None, detail=str(exc))
        except (ValueError, OSError) as exc:
            status = EnvironmentStatus(state="failed", tag=None, detail=str(exc))
            self._status[project_id] = status
            return status
        status = EnvironmentStatus(
            state="ready" if exists else "none", tag=inputs.tag if exists else None, detail=None
        )
        self._status[project_id] = status
        return status

    async def _record_build_end(self, project_id: str, status: EnvironmentStatus) -> None:
        event_type = "sandbox.build.finished" if status.state == "ready" else "sandbox.build.failed"
        payload = {
            "tag": status.tag,
            "detail": None if status.state == "ready" else status.detail,
        }
        pending = asyncio.create_task(
            self._events.record(
                project_id,
                source="system",
                type=event_type,
                payload=payload,
            )
        )
        cancelled: asyncio.CancelledError | None = None
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        try:
            pending.result()
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            logger.exception("Failed to record %s event for project %s", event_type, project_id)

        if cancelled is not None:
            raise cancelled

    async def _build_project_image(self, project_id: str, worktree: Path) -> EnvironmentStatus:
        """Own the full build lifecycle, including dependency reads and context creation."""
        lock = self._build_locks.setdefault(project_id, asyncio.Lock())
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
                try:
                    status = await self._image_manager.build_image(
                        project_id, worktree, inputs=inputs
                    )
                except asyncio.CancelledError as exc:
                    detail = build_failure_detail(exc)
                    logger.exception("Image build cancelled for project %s", project_id)
                    status = EnvironmentStatus(state="failed", tag=tag, detail=detail)
                    self._status[project_id] = status
                    await self._record_build_end(project_id, status)
                    raise
                except Exception as exc:
                    detail = build_failure_detail(exc)
                    logger.exception("Image build failed for project %s", project_id)
                    status = EnvironmentStatus(state="failed", tag=tag, detail=detail)
            except (OSError, ValueError) as exc:
                detail = build_failure_detail(exc)
                logger.exception("Image build failed for project %s", project_id)
                status = EnvironmentStatus(state="failed", tag=tag, detail=detail)

            self._status[project_id] = status
            await self._record_build_end(project_id, status)
            return status

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        docker_status = await self.docker()
        if not docker_status.available:
            raise SandboxUnavailable(docker_status.detail or "Docker is not available.")
        root = await self._projects.root(project_id)
        return await self._build_project_image(project_id, root)

    async def get_network(self, project_id: str) -> bool:
        return await self._projects.get_network(project_id)

    async def set_network(self, project_id: str, enabled: bool) -> bool:
        docker_status = await self.docker()
        if not docker_status.available:
            raise SandboxUnavailable(docker_status.detail or "Docker is not available.")
        async with self._exclusive_tasks(project_id):
            old = await self.get_network(project_id)
            if old != enabled:
                await _finish_operation(
                    self._container_manager.remove_project_containers(project_id)
                )
            await _finish_operation(self._projects.set_network(project_id, enabled))
        return enabled

    async def ensure_container(self, project_id: str, task: TaskRef) -> str:
        async with self._task_guard(project_id, task.id):
            return await self._ensure_container_locked(project_id, task)

    async def _ensure_container_locked(self, project_id: str, task: TaskRef) -> str:
        """The caller holds the task guard through creation and any subsequent command."""
        docker_status = await self.docker()
        if not docker_status.available:
            raise SandboxUnavailable(docker_status.detail or "Docker is not available.")

        status = await self._build_project_image(project_id, task.worktree)
        if status.state != "ready" or not status.tag:
            raise SandboxUnavailable(
                f"Environment build failed: {status.detail or 'unknown error'}"
            )
        network = await self.get_network(project_id)
        mounts = await self._projects.mounts(project_id)
        async with docker_error_boundary("ensure sandbox container"):
            return await _finish_operation(
                self._container_manager.ensure_container(
                    project_id, task, status.tag, network=network, mounts=mounts
                )
            )

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        async with self._task_guard(project_id, task.id):
            cname = await self._ensure_container_locked(project_id, task)
            timeout = (
                max(1, min(timeout_s, MAX_BASH_TIMEOUT_S))
                if timeout_s > 0
                else DEFAULT_BASH_TIMEOUT_S
            )

            try:
                async with docker_error_boundary(
                    "run sandbox command", reraise_docker_timeout=True
                ):
                    res = await self._runner.run(
                        ["exec", cname, "bash", "-lc", command],
                        timeout_s=timeout,
                    )
            except asyncio.CancelledError as exc:
                try:
                    await _finish_operation(self._restart_container(cname))
                except BaseException as cleanup_error:
                    _raise_with_cleanup(exc, cleanup_error)
                raise
            except DockerTimeoutError as exc:
                await _finish_operation(self._restart_container(cname))
                captured = exc.output
                message = f"Command took longer than {timeout} seconds and was stopped."
                output = f"{captured}\n\n{message}" if captured else message
                return CommandResult(
                    output=output,
                    exit_code=None,
                    timed_out=True,
                    truncated=exc.truncated,
                )

            output, truncated = res.command_output()
            return CommandResult(
                output=output,
                exit_code=res.exit_code,
                timed_out=False,
                truncated=truncated,
            )

    async def _restart_container(self, cname: str) -> None:
        async with docker_error_boundary(
            f"restart sandbox container {cname}",
            gerund=f"restarting sandbox container {cname}",
        ):
            restarted = await self._runner.run(["restart", "-t", "0", cname], timeout_s=30.0)
        if restarted.exit_code != 0:
            raise SandboxUnavailable(
                f"Failed to restart sandbox container: {restarted.stderr or restarted.stdout}"
            )

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None]:
        """Freeze host writes exclusively; await remove_task only after exiting this context."""
        async with self._task_guard(project_id, task_id):
            cname = container_name(task_id)
            info = await self._container_manager.inspect(cname)
            if info is None or not info.running:
                yield
                return
            if info.paused:
                await _finish_operation(self._container_manager.unpause(cname))
            frozen = False
            body_error: BaseException | None = None

            async def freeze() -> None:
                nonlocal frozen
                await self._container_manager.pause(cname)
                frozen = True

            try:
                await _finish_operation(freeze())
                yield
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                if frozen:
                    try:
                        await _finish_operation(self._container_manager.unpause(cname))
                    except BaseException as cleanup_error:
                        if body_error is not None:
                            _raise_with_cleanup(body_error, cleanup_error)
                        raise

    async def remove_task(self, project_id: str, task_id: str) -> None:
        """Wait for another caller's pause/command to end; reject same-caller nesting."""
        async with self._task_guard(project_id, task_id):
            await _finish_operation(self._container_manager.remove_task(project_id, task_id))

    async def close(self) -> None:
        docker_status = await self.docker()
        if docker_status.available:
            async with self._exclusive_tasks():
                await _finish_operation(self._container_manager.cleanup_all())

    async def cleanup_orphans(self) -> None:
        docker_status = await self.docker()
        if docker_status.available:
            async with self._exclusive_tasks():
                await _finish_operation(self._container_manager.cleanup_orphans())
