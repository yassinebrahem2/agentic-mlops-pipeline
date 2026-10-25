from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.sandbox.containers import ContainerManager
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult, detect_docker
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


@pytest.mark.docker
async def test_real_docker_recovers_stale_pause(tmp_path: Path) -> None:
    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.sandbox.docker import CliDockerRunner
    from mlops_agents.sandbox.service import SandboxService
    from tests.sandbox.fakes import FakeEventLog, MemorySandboxProjects

    runner = CliDockerRunner()
    service = SandboxService(
        runner, projects=MemorySandboxProjects({"prj_recovery": tmp_path}), events=FakeEventLog()
    )
    task = TaskRef("tsk_pause_recovery", "Recovery", "branch", tmp_path, "active")
    name = await service.ensure_container("prj_recovery", task)
    manager = ContainerManager(runner)
    try:
        await manager.pause(name)
        assert await service.ensure_container("prj_recovery", task) == name
        result = await runner.run(["exec", name, "bash", "-lc", "echo recovered"], timeout_s=10)
        assert result.exit_code == 0 and result.stdout.strip() == "recovered"
        await manager.pause(name)
        async with service.paused("prj_recovery", task.id):
            info = await manager.inspect(name)
            assert info is not None and info.paused
        info = await manager.inspect(name)
        assert info is not None and not info.paused
        await manager.pause(name)
        await service.cleanup_orphans()
        assert await manager.inspect(name) is None
    finally:
        await service.remove_task("prj_recovery", task.id)
