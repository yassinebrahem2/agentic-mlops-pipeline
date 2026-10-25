"""Sandbox service implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

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
    DOCKER_INSTALL_MESSAGE,
    CliDockerRunner,
    DockerRunner,
    DockerTimeoutError,
    detect_docker,
)
from mlops_agents.sandbox.images import ImageManager, snapshot_image
from mlops_agents.sandbox.projects import SandboxProjects


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
        try:
            return await _finish_operation(
                self._container_manager.ensure_container(
                    project_id, task, status.tag, network=network
                )
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from exc
        except OSError as exc:
            raise SandboxUnavailable(f"Sandbox container operation failed: {exc}") from exc

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
                res = await self._runner.run(
                    ["exec", cname, "bash", "-lc", command],
                    timeout_s=timeout,
                )
            except TimeoutError as exc:
                try:
                    restarted = await self._runner.run(
                        ["restart", "-t", "0", cname], timeout_s=30.0
                    )
                except FileNotFoundError as err:
                    raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from err
                except TimeoutError as err:
                    raise SandboxUnavailable("Timed out restarting sandbox container.") from err
                except OSError as err:
                    raise SandboxUnavailable(f"Sandbox container restart failed: {err}") from err
                if restarted.exit_code != 0:
                    raise SandboxUnavailable(
                        f"Failed to restart sandbox container: {restarted.stderr or restarted.stdout}"
                    ) from exc
                if isinstance(exc, DockerTimeoutError) and exc.output is not None:
                    captured, truncated = exc.output, exc.truncated
                else:
                    captured, truncated = "", False
                message = f"Command took longer than {timeout} seconds and was stopped."
                output = f"{captured}\n\n{message}" if captured else message
                return CommandResult(
                    output=output,
                    exit_code=None,
                    timed_out=True,
                    truncated=truncated,
                )
            except FileNotFoundError as err:
                raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from err
            except OSError as err:
                raise SandboxUnavailable(f"Sandbox command failed: {err}") from err

            output, truncated = res.command_output()
            return CommandResult(
                output=output,
                exit_code=res.exit_code,
                timed_out=False,
                truncated=truncated,
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
                            raise body_error from cleanup_error
                        raise

    async def remove_task(self, project_id: str, task_id: str) -> None:
        """Wait for another caller's pause/command to end; reject same-caller nesting."""
        async with self._task_guard(project_id, task_id):
            await _finish_operation(self._container_manager.remove_task(project_id, task_id))

    async def close(self) -> None:
        async with self._exclusive_tasks():
            await _finish_operation(self._container_manager.cleanup_all())

    async def cleanup_orphans(self) -> None:
        docker_status = await self.docker()
        if docker_status.available:
            async with self._exclusive_tasks():
                await _finish_operation(self._container_manager.cleanup_orphans())
