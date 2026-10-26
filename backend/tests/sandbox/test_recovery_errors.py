from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.sandbox.containers import ContainerManager
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import (
    DOCKER_INSTALL_MESSAGE,
    DockerResult,
    DockerTimeoutError,
    detect_docker,
)
from mlops_agents.sandbox.images import ImageManager
from tests.sandbox.test_task_locking import ControlledDocker, setup_service


@pytest.mark.parametrize("operation", ["ensure", "paused", "startup"])
async def test_recovers_container_left_paused_after_crash(tmp_path: Path, operation: str) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    state = cast(dict[str, object], runner.containers[name]["State"])
    state["Paused"] = True
    start = len(runner.calls)
    if operation == "ensure":
        assert await service.ensure_container("prj", task) == name
    elif operation == "paused":
        async with service.paused("prj", task.id):
            assert state["Paused"] is True
    else:
        await service.cleanup_orphans()
        assert name not in runner.containers
    assert state["Paused"] is False
    operations = [c[0] for c in runner.calls[start:]]
    assert "unpause" in operations
    if operation == "startup":
        assert operations.index("unpause") < operations.index("rm")


class FailedDocker(ControlledDocker):
    def __init__(self, operation: str, detail: str) -> None:
        super().__init__()
        self.operation = operation
        self.detail = detail

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        if args[0] == self.operation:
            return DockerResult("", self.detail, 1)
        return await super().run(args, timeout_s=timeout_s, secrets=secrets)


@pytest.mark.parametrize("detail", ["permission denied", "Cannot connect to the Docker daemon"])
async def test_inspect_failure_is_not_a_missing_container(detail: str) -> None:
    manager = ContainerManager(FailedDocker("inspect", detail))
    with pytest.raises(SandboxUnavailable, match=detail):
        await manager.inspect("container")


async def test_inspect_missing_container_is_none() -> None:
    manager = ContainerManager(FailedDocker("inspect", "Error: No such container: container"))
    assert await manager.inspect("container") is None


async def test_inspect_requests_container_type_for_dockers_missing_container_error() -> None:
    runner = ControlledDocker()
    assert await ContainerManager(runner).inspect("missing") is None
    assert runner.calls == [["inspect", "missing", "--type", "container"]]


@pytest.mark.parametrize("operation", ["ps", "rm"])
@pytest.mark.parametrize("cleanup", ["project", "all", "startup", "network"])
async def test_cleanup_failures_surface(tmp_path: Path, operation: str, cleanup: str) -> None:
    runner = FailedDocker("unused", "permission denied")
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.operation = operation
    manager = ContainerManager(runner)
    with pytest.raises(SandboxUnavailable, match="permission denied"):
        if cleanup == "project":
            await manager.remove_project_containers("prj")
        elif cleanup == "all":
            await service.close()
        elif cleanup == "startup":
            await service.cleanup_orphans()
        else:
            await service.set_network("prj", True)
    if cleanup == "network":
        assert await service.get_network("prj") is False


@pytest.mark.parametrize(
    ("detail", "daemon_down"),
    [
        ("permission denied", False),
        ("unknown flag: --format", False),
        ("Cannot connect to the Docker daemon. Is the docker daemon running?", True),
    ],
)
async def test_docker_failure_diagnosis_matches_output(detail: str, daemon_down: bool) -> None:
    status = await detect_docker(FailedDocker("version", detail))
    assert status.available is False
    assert status.detail is not None
    if daemon_down:
        assert "daemon is not running" in status.detail
    else:
        assert detail in status.detail
        assert "daemon is not running" not in status.detail


async def test_close_skips_cleanup_when_docker_unavailable(tmp_path: Path) -> None:
    class DaemonDownDocker(ControlledDocker):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] in ("version", "ps"):
                return DockerResult("", "Cannot connect to the Docker daemon", 1)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    runner = DaemonDownDocker()
    service, _ = setup_service(tmp_path, runner)
    # Calling close() with daemon down must not raise
    await service.close()

    # Calling set_network() with daemon down still fails when network changes
    with pytest.raises(SandboxUnavailable, match="Docker daemon is not running"):
        await service.set_network("prj", True)


class ErrorDocker(ControlledDocker):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        if args[0] == "inspect":
            raise self.exc
        return await super().run(args, timeout_s=timeout_s, secrets=secrets)


async def test_inspect_maps_timeout_and_filenotfound() -> None:
    manager_timeout = ContainerManager(
        ErrorDocker(DockerTimeoutError("timeout", output="", truncated=False))
    )
    with pytest.raises(SandboxUnavailable, match="Timed out inspecting sandbox container"):
        await manager_timeout.inspect("c1")

    manager_notfound = ContainerManager(
        ErrorDocker(FileNotFoundError(2, "No such file or directory"))
    )
    with pytest.raises(SandboxUnavailable, match=re.escape(DOCKER_INSTALL_MESSAGE)):
        await manager_notfound.inspect("c1")


async def test_paused_maps_inspect_timeout(tmp_path: Path) -> None:
    runner = ErrorDocker(DockerTimeoutError("timeout", output="", truncated=False))
    service, task = setup_service(tmp_path, runner)
    with pytest.raises(SandboxUnavailable, match="Timed out inspecting sandbox container"):
        async with service.paused("prj", task.id):
            pass


async def test_image_exists_raises_on_daemon_error() -> None:
    runner = FailedDocker("image", "Cannot connect to the Docker daemon")
    manager = ImageManager(runner)
    with pytest.raises(SandboxUnavailable, match="Cannot connect to the Docker daemon"):
        await manager.image_exists("some-tag")


async def test_image_exists_returns_false_only_on_no_such_image() -> None:
    runner = FailedDocker("image", "Error: No such image: some-tag")
    manager = ImageManager(runner)
    assert await manager.image_exists("some-tag") is False


async def test_build_image_maps_filenotfound_without_filename(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    class MissingDocker(ControlledDocker):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "image":
                return DockerResult("", "no such image", 1)
            if args[0] == "build":
                # On Windows, exc.filename is None for CreateProcess FileNotFoundError
                raise FileNotFoundError(2, "No such file or directory", None)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    manager = ImageManager(MissingDocker())
    status = await manager.build_image("prj_1", worktree)
    assert status.state == "failed"
    assert status.detail == DOCKER_INSTALL_MESSAGE


async def test_paused_preserves_body_exception_cause(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)

    original_run = runner.run

    async def fail_unpause(
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        if args[0] == "unpause":
            raise OSError("unpause failed")
        return await original_run(args, timeout_s=timeout_s, secrets=secrets)

    runner.run = fail_unpause

    inner_cause = ValueError("original root cause")
    with pytest.raises(RuntimeError) as exc_info:
        async with service.paused("prj", task.id):
            raise RuntimeError("body failed") from inner_cause

    # The body error's cause must NOT have been overwritten by unpause's failure
    assert exc_info.value.__cause__ is inner_cause
    assert any("Cleanup failed" in note for note in getattr(exc_info.value, "__notes__", []))


async def test_paused_chains_cleanup_error_when_body_has_no_cause(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)

    original_run = runner.run

    async def fail_unpause(
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        if args[0] == "unpause":
            raise OSError("unpause failed")
        return await original_run(args, timeout_s=timeout_s, secrets=secrets)

    runner.run = fail_unpause

    with pytest.raises(RuntimeError) as exc_info:
        async with service.paused("prj", task.id):
            raise RuntimeError("body without cause")

    assert isinstance(exc_info.value.__cause__, SandboxUnavailable)
    assert "unpause failed" in str(exc_info.value.__cause__)


async def test_paused_container_removal_paths(tmp_path: Path) -> None:
    # 1. ensure_container mismatch path removes paused container
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    runner.containers[name]["State"] = {"Running": True, "Paused": True}
    manager = ContainerManager(runner)
    await manager.ensure_container("prj", task, "new-image-tag", network=False)
    rm_calls = [c for c in runner.calls if c[0] == "rm" and name in c]
    assert len(rm_calls) >= 1

    # 2. start() path removes paused container
    runner.containers[name]["State"] = {"Running": True, "Paused": True}
    start_calls_before = len(runner.calls)
    await manager.start("prj", task, "another-tag", network=False)
    start_rm_calls = [c for c in runner.calls[start_calls_before:] if c[0] == "rm" and name in c]
    assert len(start_rm_calls) >= 1

    # 3. remove_task() path removes paused container
    runner.containers[name]["State"] = {"Running": True, "Paused": True}
    remove_calls_before = len(runner.calls)
    await manager.remove_task("prj", task.id)
    task_rm_calls = [c for c in runner.calls[remove_calls_before:] if c[0] == "rm" and name in c]
    assert len(task_rm_calls) >= 1


async def test_environment_returns_unavailable_when_docker_missing(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    class MissingDockerRunner(ControlledDocker):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] in ("version", "image"):
                raise FileNotFoundError(2, "No such file or directory: docker")
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    service, _ = setup_service(tmp_path, MissingDockerRunner())
    status = await service.environment("prj")
    assert status.state == "unavailable"
    assert status.tag is None
    assert status.detail is not None
    assert "Docker is not installed" in status.detail
