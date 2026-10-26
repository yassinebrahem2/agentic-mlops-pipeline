from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import CommandResult, SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult, StreamCapture
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


async def test_run_missing_docker(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.docker_installed = False
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_1",
        title="Task Run 1",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    with pytest.raises(SandboxUnavailable, match="Docker is not installed"):
        await service.run("prj_1", task, "echo hi", 10)


async def test_run_daemon_down(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.daemon_running = False
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_2",
        title="Task Run 2",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    with pytest.raises(SandboxUnavailable, match="Docker daemon is not running"):
        await service.run("prj_1", task, "echo hi", 10)


async def test_run_build_image_failure(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.build_fail_detail = "Step 4/5 : RUN pip install broken\nERROR: No matching distribution"
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_3",
        title="Task Run 3",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    with pytest.raises(SandboxUnavailable, match="Environment build failed:"):
        await service.run("prj_1", task, "echo hi", 10)


async def test_run_successful_command(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.exec_result = DockerResult(stdout="Hello from container\n", stderr="", exit_code=0)
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_4",
        title="Task Run 4",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    result = await service.run("prj_1", task, "echo 'Hello from container'", 30)
    assert result == CommandResult(
        output="Hello from container\n",
        exit_code=0,
        timed_out=False,
        truncated=False,
    )
    # Check docker exec arguments
    exec_calls = [c for c in runner.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    assert exec_calls[0] == [
        "exec",
        f"{APP_NAME}-task_run_4",
        "bash",
        "-lc",
        "echo 'Hello from container'",
    ]


async def test_run_non_zero_exit_code(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.exec_result = DockerResult(stdout="", stderr="syntax error\n", exit_code=2)
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_5",
        title="Task Run 5",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    result = await service.run("prj_1", task, "invalid command", 30)
    assert result == CommandResult(
        output="syntax error\n",
        exit_code=2,
        timed_out=False,
        truncated=False,
    )


async def test_run_bounded_capture_1mb_output(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    # 1 MB of characters
    head_char = "A" * 15_000
    middle_char = "M" * 970_000
    tail_char = "Z" * 15_000
    big_output = head_char + middle_char + tail_char  # total 1,000,000 chars

    cap = StreamCapture()
    cap.feed(big_output)
    runner.exec_result = DockerResult(
        stdout=big_output,
        stderr="",
        exit_code=0,
        output=cap.get_output(),
        truncated=cap.truncated,
    )
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_6",
        title="Task Run 6",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    result = await service.run("prj_1", task, "generate big data", 30)
    assert result.truncated is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.output.startswith(head_char)
    assert result.output.endswith(tail_char)
    assert "[... 970000 characters truncated ...]" in result.output
    # Output length should be 30,000 + length of the truncation message
    assert len(result.output) < 30_100


async def test_run_preserves_runner_capture_and_truncation_count(tmp_path: Path) -> None:
    import sys

    from mlops_agents.sandbox.docker import CliDockerRunner

    runner = FakeDockerRunner()
    runner.exec_result = await CliDockerRunner(sys.executable).run(
        ["-c", "print('A'*15000 + 'M'*970000 + 'Z'*15000, end='')"]
    )
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())
    task = TaskRef("tsk_capture", "Capture", "branch", tmp_path, "active")
    result = await service.run("prj_1", task, "produce", 30)
    assert result.truncated
    assert result.output.startswith("A" * 15000)
    assert result.output.endswith("Z" * 15000)
    assert "970000 characters truncated" in result.output
    assert result.output.count("characters truncated") == 1


async def test_run_timeout_restarts_container(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.exec_timeout = True
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_7",
        title="Task Run 7",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    result = await service.run("prj_1", task, "sleep 100", 15)
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.truncated is False
    assert "Command took longer than 15 seconds and was stopped." in result.output

    # Verify restart with -t 0 was called
    restart_calls = [c for c in runner.calls if c[0] == "restart"]
    assert len(restart_calls) == 1
    assert restart_calls[0] == ["restart", "-t", "0", f"{APP_NAME}-task_run_7"]


async def test_run_timeout_returns_captured_output(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.exec_timeout = True
    runner.exec_timeout_output = "processing step 1\nprocessing step 2"
    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())

    task = TaskRef(
        id="task_run_timeout_captured",
        title="Task Run Timeout Captured",
        branch="branch-1",
        worktree=tmp_path,
        status="active",
    )
    result = await service.run("prj_1", task, "sleep 100", 15)
    assert result.timed_out is True
    assert result.exit_code is None
    assert "processing step 1\nprocessing step 2" in result.output
    assert "Command took longer than 15 seconds and was stopped." in result.output
