from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


async def test_service_remove_task_and_close(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )

    task = TaskRef(
        id="task_svc_1",
        title="Task Svc",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )

    cname = await service.ensure_container("prj_1", task)
    assert cname == f"{APP_NAME}-task_svc_1"
    assert cname in runner.containers

    # Remove task
    await service.remove_task("prj_1", "task_svc_1")
    assert cname not in runner.containers

    # Ensure container again
    await service.ensure_container("prj_1", task)
    assert cname in runner.containers

    # Close cleans up all containers
    await service.close()
    assert cname not in runner.containers


async def test_service_network_settings_and_container_recreation(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )

    task = TaskRef(
        id="task_net_1",
        title="Task Net",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )

    # Default network is False
    assert await service.get_network("prj_1") is False

    # Start container with default network=False
    cname = await service.ensure_container("prj_1", task)
    c1_host = cast(dict[str, object], runner.containers[cname]["HostConfig"])
    assert c1_host["NetworkMode"] == "none"

    # Update network setting to True
    res = await service.set_network("prj_1", True)
    assert res is True
    assert await service.get_network("prj_1") is True

    # Changing network setting stops and removes running containers for prj_1
    assert cname not in runner.containers

    # Calling ensure_container again starts it with network enabled
    cname2 = await service.ensure_container("prj_1", task)
    assert cname2 == cname
    c2_host = cast(dict[str, object], runner.containers[cname]["HostConfig"])
    assert c2_host["NetworkMode"] == "default"


async def test_service_cleanup_orphans(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    # Pre-populate an orphan container
    runner.containers[f"{APP_NAME}-orphan"] = {
        "Id": "cid_orphan",
        "Name": f"/{APP_NAME}-orphan",
        "State": {"Running": True, "Status": "running"},
        "Config": {"Image": "some_img", "Labels": {f"{APP_NAME}": "true"}},
    }

    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())
    await service.cleanup_orphans()
    assert f"{APP_NAME}-orphan" not in runner.containers


@pytest.mark.parametrize("operation", ["inspect", "rm", "run"])
@pytest.mark.parametrize("error", [TimeoutError("operation timed out"), OSError("I/O failed")])
async def test_container_operational_failure_becomes_unavailable(
    tmp_path: Path,
    operation: str,
    error: OSError,
) -> None:
    class FailedOperation(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == operation:
                raise error
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    service = SandboxService(
        FailedOperation(), projects=MemorySandboxProjects(), events=FakeEventLog()
    )
    task = TaskRef("tsk_container_failure", "Failure", "branch", tmp_path, "active")
    with pytest.raises(SandboxUnavailable):
        await service.ensure_container("prj_1", task)


async def test_unexpected_container_failure_surfaces(tmp_path: Path) -> None:
    class BrokenRunner(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "inspect":
                raise RuntimeError("runner defect")
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    service = SandboxService(
        BrokenRunner(), projects=MemorySandboxProjects(), events=FakeEventLog()
    )
    task = TaskRef("tsk_unexpected", "Failure", "branch", tmp_path, "active")
    with pytest.raises(RuntimeError, match="runner defect"):
        await service.ensure_container("prj_1", task)


async def test_ensure_container_failed_build_raises_unavailable_with_detail(tmp_path: Path) -> None:
    class FailingBuildDocker(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "build":
                return DockerResult(
                    stdout="",
                    stderr="ERROR: failed to solve: syntax error at line 5",
                    exit_code=1,
                )
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("badpkg\n", encoding="utf-8")
    service = SandboxService(
        runner=FailingBuildDocker(),
        projects=MemorySandboxProjects({"prj_1": worktree}),
        events=FakeEventLog(),
    )
    task = TaskRef("tsk_build_fail", "Build Fail", "branch", worktree, "active")

    with pytest.raises(SandboxUnavailable) as exc_info:
        await service.ensure_container("prj_1", task)
    assert "syntax error at line 5" in str(exc_info.value)


async def test_service_paused_context_manager(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )
    task = TaskRef("tsk_pause_1", "Task Pause", "branch", worktree, "active")

    # 1. When container does not exist, paused is a clean no-op
    calls_before = len(runner.calls)
    async with service.paused("prj_1", task.id):
        pass
    assert not any(c[0] in ("pause", "unpause") for c in runner.calls[calls_before:])

    # 2. When container exists and is running
    cname = await service.ensure_container("prj_1", task)
    assert cname in runner.containers

    async with service.paused("prj_1", task.id):
        c_state = cast(dict[str, object], runner.containers[cname]["State"])
        assert c_state.get("Paused") is True
        assert any(c[0] == "pause" for c in runner.calls)

    # After exiting context, container is unpaused
    c_state_after = cast(dict[str, object], runner.containers[cname]["State"])
    assert c_state_after.get("Paused") is False
    assert any(c[0] == "unpause" for c in runner.calls)


async def test_service_task_lock_serializes_paused(tmp_path: Path) -> None:
    import asyncio

    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )
    task = TaskRef("tsk_lock_1", "Lock Task", "branch", worktree, "active")
    await service.ensure_container("prj_1", task)

    events_sequence: list[str] = []
    first_entered = asyncio.Event()
    second_waiting = asyncio.Event()

    async def pause_block_1() -> None:
        async with service.paused("prj_1", task.id):
            events_sequence.append("p1_started")
            first_entered.set()
            await second_waiting.wait()
            events_sequence.append("p1_ending")

    async def pause_block_2() -> None:
        await first_entered.wait()
        events_sequence.append("p2_waiting")
        second_waiting.set()
        async with service.paused("prj_1", task.id):
            events_sequence.append("p2_started")

    await asyncio.gather(pause_block_1(), pause_block_2())
    assert events_sequence == [
        "p1_started",
        "p2_waiting",
        "p1_ending",
        "p2_started",
    ]
