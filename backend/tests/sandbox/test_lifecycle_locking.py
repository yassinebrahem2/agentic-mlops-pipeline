from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult
from tests.sandbox.test_task_locking import ControlledDocker, setup_service


async def test_cancelled_recovery_settles_before_host_write(tmp_path: Path) -> None:
    class DelayedDaemon(ControlledDocker):
        daemon: asyncio.Task[DockerResult] | None = None

        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "unpause" and self.daemon is None:
                self.daemon = asyncio.create_task(
                    super().run(args, timeout_s=timeout_s, secrets=secrets)
                )
                return await asyncio.shield(self.daemon)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    runner = DelayedDaemon()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    state = cast(dict[str, object], runner.containers[name]["State"])
    state["Paused"] = True
    runner.block_operation = "unpause"
    recovering = asyncio.create_task(service.ensure_container("prj", task))
    await asyncio.wait_for(runner.entered.wait(), 1)
    recovering.cancel()
    await asyncio.sleep(0)
    runner.block_operation = None
    entered = asyncio.Event()
    release_write = asyncio.Event()

    async def write() -> None:
        async with service.paused("prj", task.id):
            entered.set()
            await release_write.wait()
            assert state["Paused"] is True

    writing = asyncio.create_task(write())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not recovering.done()
        assert not entered.is_set()
    finally:
        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await recovering
        if runner.daemon is not None:
            await runner.daemon
        release_write.set()
        await writing


@pytest.mark.parametrize("cleanup", ["network", "close", "startup"])
async def test_cleanup_waits_for_active_host_write(tmp_path: Path, cleanup: str) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    state = cast(dict[str, object], runner.containers[name]["State"])
    async with service.paused("prj", task.id):
        pending = asyncio.create_task(
            service.set_network("prj", True)
            if cleanup == "network"
            else service.close()
            if cleanup == "close"
            else service.cleanup_orphans()
        )
        await asyncio.sleep(0)
        assert not pending.done()
        assert state["Paused"] is True
        assert name in runner.containers
    await pending
    assert name not in runner.containers


async def test_new_task_waits_until_network_cleanup_finishes(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.block_operation = "rm"
    changing = asyncio.create_task(service.set_network("prj", True))
    await asyncio.wait_for(runner.entered.wait(), 1)
    other = TaskRef("tsk_other", "Other", "branch", tmp_path, "active")
    creating = asyncio.create_task(service.ensure_container("prj", other))
    try:
        await asyncio.sleep(0)
        assert not creating.done()
        assert len([c for c in runner.calls if c[0] == "run"]) == 1
    finally:
        runner.release.set()
        await asyncio.gather(changing, creating)
    last_run = [c for c in runner.calls if c[0] == "run"][-1]
    assert "--network" not in last_run


async def test_nested_operation_fails_before_cleanup_gate_can_deadlock(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    other = TaskRef("tsk_nested", "Nested", "branch", tmp_path, "active")
    async with asyncio.timeout(1):
        async with service.paused("prj", task.id):
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            with pytest.raises(SandboxUnavailable, match="after leaving paused"):
                await service.ensure_container("prj", other)
            with pytest.raises(SandboxUnavailable, match="after leaving paused"):
                await service.set_network("prj", True)
        await closing


async def test_cancelled_cleanup_finishes_before_task_admission(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    runner.block_operation = "rm"
    closing = asyncio.create_task(service.close())
    await asyncio.wait_for(runner.entered.wait(), 1)
    closing.cancel()
    await asyncio.sleep(0)
    creating = asyncio.create_task(service.ensure_container("prj", task))
    try:
        await asyncio.sleep(0)
        assert not creating.done()
    finally:
        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await creating
    assert name in runner.containers
    assert [c[0] for c in runner.calls].count("run") == 2
