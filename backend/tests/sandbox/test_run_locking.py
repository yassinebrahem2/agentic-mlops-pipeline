from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.sandbox.docker import DockerResult, DockerTimeoutError
from tests.sandbox.test_task_locking import ControlledDocker, setup_service


async def test_bash_waits_until_paused_context_exits(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    async with service.paused("prj", task.id):
        command = asyncio.create_task(service.run("prj", task, "echo hello", 10))
        await asyncio.sleep(0)
        assert not command.done()
        assert not any(c[0] == "exec" for c in runner.calls)
    result = await command
    assert result.exit_code == 0
    operations = [c[0] for c in runner.calls]
    assert operations.index("unpause") < operations.index("exec")


async def test_paused_waits_until_bash_exits(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.block_operation = "exec"
    command = asyncio.create_task(service.run("prj", task, "echo hello", 10))
    await asyncio.wait_for(runner.entered.wait(), 1)
    paused_entered = asyncio.Event()

    async def write() -> None:
        async with service.paused("prj", task.id):
            paused_entered.set()

    writing = asyncio.create_task(write())
    try:
        await asyncio.sleep(0)
        assert not paused_entered.is_set()
        assert not any(c[0] == "pause" for c in runner.calls)
    finally:
        runner.release.set()
        await asyncio.gather(command, writing)
    operations = [c[0] for c in runner.calls]
    assert operations.index("exec") < operations.index("pause")
    assert paused_entered.is_set()


async def test_bash_recovers_paused_container_before_exec(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    name = await service.ensure_container("prj", task)
    state = cast(dict[str, object], runner.containers[name]["State"])
    state["Paused"] = True
    result = await service.run("prj", task, "echo recovered", 10)
    assert result.exit_code == 0
    assert state["Paused"] is False
    operations = [c[0] for c in runner.calls]
    assert operations.index("unpause") < operations.index("exec")


async def test_timed_out_bash_restarts_before_second_command_starts(tmp_path: Path) -> None:
    class TimeoutDocker(ControlledDocker):
        first = True

        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            result = await super().run(args, timeout_s=timeout_s, secrets=secrets)
            if args[0] == "exec" and self.first:
                self.first = False
                raise DockerTimeoutError("timed out", output="partial output", truncated=True)
            return result

    runner = TimeoutDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.block_operation = "exec"
    first = asyncio.create_task(service.run("prj", task, "sleep 100", 1))
    await asyncio.wait_for(runner.entered.wait(), 1)
    second = asyncio.create_task(service.run("prj", task, "echo ready", 10))
    try:
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        runner.release.set()
        timed_out, completed = await asyncio.gather(first, second)
    assert timed_out.timed_out and timed_out.truncated
    assert timed_out.output.startswith("partial output")
    assert completed.exit_code == 0
    operations = [c[0] for c in runner.calls if c[0] in ("exec", "restart")]
    assert operations == ["exec", "restart", "exec"]


async def test_cancelled_bash_restarts_before_waiting_pause_enters(tmp_path: Path) -> None:
    runner = ControlledDocker()
    service, task = setup_service(tmp_path, runner)
    await service.ensure_container("prj", task)
    runner.block_operation = "exec"
    command = asyncio.create_task(service.run("prj", task, "sleep 100", 120))
    await asyncio.wait_for(runner.entered.wait(), 1)
    runner.entered.clear()
    runner.block_operation = "restart"
    command.cancel()
    paused_entered = asyncio.Event()

    async def write() -> None:
        async with service.paused("prj", task.id):
            paused_entered.set()

    writing = asyncio.create_task(write())
    try:
        await asyncio.wait_for(runner.entered.wait(), 1)
        command.cancel()
        await asyncio.sleep(0)
        assert not paused_entered.is_set()
    finally:
        runner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await command
        await writing
    operations = [c[0] for c in runner.calls]
    assert operations.index("restart") < operations.index("pause")
