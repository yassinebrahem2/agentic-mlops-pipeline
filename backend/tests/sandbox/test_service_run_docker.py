from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.agents.tools.read import ReadArgs, ReadTool
from mlops_agents.sandbox.service import SandboxService
from tests.agents.ctx import tool_context
from tests.agents.tools.test_bash import MockFileAccess
from tests.sandbox.fakes import FakeEventLog, MemorySandboxProjects


@pytest.mark.docker
async def test_docker_echo(tmp_path: Path) -> None:
    service = SandboxService(projects=MemorySandboxProjects(), events=FakeEventLog())
    task = TaskRef(
        id="docker_test_echo",
        title="Docker Echo Task",
        branch="b_echo",
        worktree=tmp_path,
        status="active",
    )
    try:
        res = await service.run("prj_docker", task, "echo 'hello from docker'", 60)
        assert res.exit_code == 0
        assert res.timed_out is False
        assert "hello from docker" in res.output
    finally:
        await service.remove_task("prj_docker", "docker_test_echo")
        await service.close()


@pytest.mark.docker
async def test_docker_failing_command(tmp_path: Path) -> None:
    service = SandboxService(projects=MemorySandboxProjects(), events=FakeEventLog())
    task = TaskRef(
        id="docker_test_fail",
        title="Docker Fail Task",
        branch="b_fail",
        worktree=tmp_path,
        status="active",
    )
    try:
        res = await service.run("prj_docker", task, "exit 42", 60)
        assert res.exit_code == 42
        assert res.timed_out is False
    finally:
        await service.remove_task("prj_docker", "docker_test_fail")
        await service.close()


@pytest.mark.docker
async def test_docker_file_written_readable_by_read_tool(tmp_path: Path) -> None:
    service = SandboxService(projects=MemorySandboxProjects(), events=FakeEventLog())
    task = TaskRef(
        id="docker_test_file",
        title="Docker File Task",
        branch="b_file",
        worktree=tmp_path,
        status="active",
    )
    try:
        res = await service.run(
            "prj_docker",
            task,
            "echo 'written_by_sandbox_bash' > test_file.txt",
            60,
        )
        assert res.exit_code == 0

        # Now verify file in worktree is readable
        assert (tmp_path / "test_file.txt").is_file()
        content = (tmp_path / "test_file.txt").read_text(encoding="utf-8")
        assert "written_by_sandbox_bash" in content

        # Read via ReadTool
        mock_files = MockFileAccess(tmp_path)
        ctx = tool_context(mock_files)
        read_tool = ReadTool()
        read_res = await read_tool.run(ctx, ReadArgs(file_path="test_file.txt"))
        assert "written_by_sandbox_bash" in read_res.content
    finally:
        await service.remove_task("prj_docker", "docker_test_file")
        await service.close()


@pytest.mark.docker
async def test_docker_timeout_restarts_container(tmp_path: Path) -> None:
    service = SandboxService(projects=MemorySandboxProjects(), events=FakeEventLog())
    task = TaskRef(
        id="docker_test_timeout",
        title="Docker Timeout Task",
        branch="b_timeout",
        worktree=tmp_path,
        status="active",
    )
    try:
        res = await service.run("prj_docker", task, "sleep 10", 2)
        assert res.timed_out is True
        assert res.exit_code is None
        assert "Command took longer than 2 seconds and was stopped." in res.output
    finally:
        await service.remove_task("prj_docker", "docker_test_timeout")
        await service.close()
