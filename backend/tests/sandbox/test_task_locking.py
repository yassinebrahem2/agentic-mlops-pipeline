from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


class ControlledDocker(FakeDockerRunner):
    def __init__(self) -> None:
        super().__init__()
        self.block_operation: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_unpause = False

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        if args[0] == self.block_operation:
            self.entered.set()
            await self.release.wait()
        if args[0] == "unpause" and self.fail_unpause:
            return DockerResult("", "unpause refused", 1)
        if args[0] == "pause":
            state = cast(dict[str, object], self.containers[args[1]]["State"])
            if state.get("Paused"):
                return DockerResult("", "already paused", 1)
        return await super().run(args, timeout_s=timeout_s, secrets=secrets)


def setup_service(path: Path, runner: ControlledDocker) -> tuple[SandboxService, TaskRef]:
    return (
        SandboxService(
            runner, projects=MemorySandboxProjects({"prj": path}), events=FakeEventLog()
        ),
        TaskRef("tsk_lock", "Lock", "branch", path, "active"),
    )


async def test_remove_waits_for_pause_and_queued_callers_keep_same_lock(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def queued_pause() -> None:
        async with service.paused("prj", task.id):
            order.append("queued")
            entered.set()
            await release.wait()

    async def late_pause() -> None:
        async with service.paused("prj", task.id):
            order.append("late")

    async with service.paused("prj", task.id):
        removing = asyncio.create_task(service.remove_task("prj", task.id))
        await asyncio.sleep(0)
        queued = asyncio.create_task(queued_pause())
        await asyncio.sleep(0)
        assert not removing.done()
        assert name in runner.containers
    await removing
    await asyncio.wait_for(entered.wait(), 1)
    late = asyncio.create_task(late_pause())
    try:
        await asyncio.sleep(0)
        assert not late.done()
        assert order == ["queued"]
    finally:
        release.set()
        await asyncio.gather(queued, late)
    assert order == ["queued", "late"]
    assert [c[0] for c in runner.calls[-3:]] == ["rm", "inspect", "inspect"]


async def test_remove_inside_pause_fails_fast_instead_of_deadlocking(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    async with asyncio.timeout(1):
        async with service.paused("prj", task.id):
            with pytest.raises(SandboxUnavailable, match=r"after.*paused"):
                await service.remove_task("prj", task.id)
    await service.remove_task("prj", task.id)


async def test_ensure_waits_for_pause(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    async with service.paused("prj", task.id):
        calls = len(runner.calls)
        ensure = asyncio.create_task(service.ensure_container("prj", task))
        await asyncio.sleep(0)
        assert not ensure.done()
        assert len(runner.calls) == calls
    await ensure


async def test_body_error_remains_primary_when_unpause_fails(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.fail_unpause = True
    with pytest.raises(ValueError, match="host write failed") as caught:
        async with service.paused("prj", task.id):
            raise ValueError("host write failed")
    assert isinstance(caught.value.__cause__, SandboxUnavailable)
    assert "unpause refused" in str(caught.value.__cause__)


async def test_unpause_failure_without_body_error_surfaces(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.fail_unpause = True
    with pytest.raises(SandboxUnavailable, match="unpause refused"):
        async with service.paused("prj", task.id):
            pass


async def test_cancelled_removal_settles_before_container_can_be_recreated(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    runner.block_operation = "rm"
    removal = asyncio.create_task(service.remove_task("prj", task.id))
    await asyncio.wait_for(runner.entered.wait(), 1)
    removal.cancel()
    await asyncio.sleep(0)
    ensure = asyncio.create_task(service.ensure_container("prj", task))
    try:
        await asyncio.sleep(0)
        assert not ensure.done()
    finally:
        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await removal
        await ensure
    assert name in runner.containers
    assert [c[0] for c in runner.calls].count("run") == 2


@pytest.mark.parametrize("operation", ["pause", "unpause"])
async def test_cancelled_pause_finishes_unpause_before_unlocking(
    tmp_path: Path,
    operation: str,
) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    runner.block_operation = operation

    async def writing() -> None:
        async with service.paused("prj", task.id):
            pass

    writer = asyncio.create_task(writing())
    await asyncio.wait_for(runner.entered.wait(), 1)
    writer.cancel()
    await asyncio.sleep(0)
    writer.cancel()
    removing = asyncio.create_task(service.remove_task("prj", task.id))
    try:
        await asyncio.sleep(0)
        assert not removing.done()
    finally:
        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
        await removing
    operations = [c[0] for c in runner.calls]
    assert operations[-3:] == ["pause", "unpause", "rm"]
    assert name not in runner.containers
