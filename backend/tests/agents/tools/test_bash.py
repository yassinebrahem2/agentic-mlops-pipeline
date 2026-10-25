from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import (
    FileAccess,
    RiskClass,
    TaskRef,
    ToolContext,
    ToolFailure,
)
from mlops_agents.agents.tools.bash import BashArgs, BashTool
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)


class FakeSandbox(Sandbox):
    def __init__(self, result: CommandResult | Exception | None = None) -> None:
        self.result = (
            result
            if result is not None
            else CommandResult(output="ok\n", exit_code=0, timed_out=False, truncated=False)
        )
        self.run_calls: list[tuple[str, TaskRef, str, int]] = []

    async def docker(self) -> DockerStatus:
        return DockerStatus(available=True, version="27.0.0", detail=None)

    async def environment(self, project_id: str) -> EnvironmentStatus:
        return EnvironmentStatus(state="ready", tag="img:tag", detail=None)

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        return EnvironmentStatus(state="ready", tag="img:tag", detail=None)

    async def run(
        self, project_id: str, task: TaskRef, command: str, timeout_s: int
    ) -> CommandResult:
        self.run_calls.append((project_id, task, command, timeout_s))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def remove_task(self, project_id: str, task_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


class MockFileAccess(FileAccess):
    def __init__(self, root: Path) -> None:
        self._root = root
        self.invalidated = False

    @property
    def root(self) -> Path:
        return self._root

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return ""

    def invalidate(self) -> None:
        self.invalidated = True


def make_context(
    tmp_path: Path, task: TaskRef | None = None, file_access: FileAccess | None = None
) -> ToolContext:
    files = file_access if file_access is not None else MockFileAccess(tmp_path)
    return ToolContext(
        project_id="prj_1",
        agent="agent_1",
        session_id="ses_1",
        turn_id="trn_1",
        files=files,
        task=task,
    )


def test_bash_tool_metadata_and_schema() -> None:
    sandbox = FakeSandbox()
    tool = BashTool(sandbox=sandbox)
    assert tool.name == "Bash"
    assert tool.risk == RiskClass.RUN
    assert "sandbox container" in tool.description.lower()
    schema = tool.schema()
    params = schema["function"]["parameters"]
    assert "command" in params["required"]
    props = params["properties"]
    assert "command" in props
    assert "timeout" in props
    assert props["timeout"]["default"] is None
    assert "description" in props


async def test_bash_tool_preview(tmp_path: Path) -> None:
    tool = BashTool(sandbox=FakeSandbox())
    ctx = make_context(tmp_path)
    args = BashArgs(command="echo 'test'")
    prev = await tool.preview(ctx, args)
    assert prev.kind == "command"
    assert prev.title == "Run `echo 'test'`"
    assert prev.body == "echo 'test'"

    # Long command preview truncation
    long_cmd = "a" * 100
    prev_long = await tool.preview(ctx, BashArgs(command=long_cmd))
    assert len(prev_long.title) < 70
    assert "..." in prev_long.title
    assert prev_long.body == long_cmd


async def test_bash_tool_no_task_error(tmp_path: Path) -> None:
    tool = BashTool(sandbox=FakeSandbox())
    ctx = make_context(tmp_path, task=None)
    with pytest.raises(ToolFailure, match="Start a task first with StartTask"):
        await tool.run(ctx, BashArgs(command="ls"))

    res = await tool.invoke(ctx, {"command": "ls"})
    assert res.is_error is True
    assert "Start a task first with StartTask." in res.content


async def test_bash_tool_sandbox_unavailable_converts_to_tool_failure(tmp_path: Path) -> None:
    sandbox = FakeSandbox(result=SandboxUnavailable("Docker daemon is not running."))
    tool = BashTool(sandbox=sandbox)
    task = TaskRef(
        id="tsk_1",
        title="Task 1",
        branch="b1",
        worktree=tmp_path,
        status="active",
    )
    mock_files = MockFileAccess(tmp_path)
    ctx = make_context(tmp_path, task=task, file_access=mock_files)

    with pytest.raises(ToolFailure, match=r"Docker daemon is not running\."):
        await tool.run(ctx, BashArgs(command="ls"))

    assert mock_files.invalidated is True

    res = await tool.invoke(ctx, {"command": "ls"})
    assert res.is_error is True
    assert "Docker daemon is not running." in res.content


async def test_bash_tool_successful_command(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        result=CommandResult(
            output="file1.txt\nfile2.txt\n", exit_code=0, timed_out=False, truncated=False
        )
    )
    tool = BashTool(sandbox=sandbox)
    task = TaskRef(
        id="tsk_1",
        title="Task 1",
        branch="b1",
        worktree=tmp_path,
        status="active",
    )
    mock_files = MockFileAccess(tmp_path)
    ctx = make_context(tmp_path, task=task, file_access=mock_files)

    out = await tool.run(ctx, BashArgs(command="ls -la", timeout=60))
    assert out.is_error is False
    assert out.content == "file1.txt\nfile2.txt\n"
    assert mock_files.invalidated is True
    assert len(sandbox.run_calls) == 1
    assert sandbox.run_calls[0] == ("prj_1", task, "ls -la", 60)


async def test_bash_tool_non_zero_exit_code(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        result=CommandResult(
            output="ls: cannot access 'nonexistent': No such file or directory\n",
            exit_code=2,
            timed_out=False,
            truncated=False,
        )
    )
    tool = BashTool(sandbox=sandbox)
    task = TaskRef(
        id="tsk_1",
        title="Task 1",
        branch="b1",
        worktree=tmp_path,
        status="active",
    )
    mock_files = MockFileAccess(tmp_path)
    ctx = make_context(tmp_path, task=task, file_access=mock_files)

    out = await tool.run(ctx, BashArgs(command="ls nonexistent"))
    assert out.is_error is False
    assert "Command failed with exit code 2:" in out.content
    assert "No such file or directory" in out.content
    assert mock_files.invalidated is True


async def test_bash_tool_timeout(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        result=CommandResult(
            output="Command took longer than 120 seconds and was stopped.",
            exit_code=None,
            timed_out=True,
            truncated=False,
        )
    )
    tool = BashTool(sandbox=sandbox)
    task = TaskRef(
        id="tsk_1",
        title="Task 1",
        branch="b1",
        worktree=tmp_path,
        status="active",
    )
    mock_files = MockFileAccess(tmp_path)
    ctx = make_context(tmp_path, task=task, file_access=mock_files)

    out = await tool.run(ctx, BashArgs(command="sleep 200"))
    assert out.is_error is False
    assert "Command took longer than 120 seconds and was stopped." in out.content
    assert mock_files.invalidated is True


def test_default_registry_with_and_without_sandbox() -> None:
    reg_without = default_registry()
    assert "Bash" not in reg_without.names()

    sandbox = FakeSandbox()
    reg_with = default_registry(sandbox=sandbox)
    assert "Bash" in reg_with.names()
    assert isinstance(reg_with.get("Bash"), BashTool)


@pytest.mark.parametrize("phase", ["snapshot", "context", "build", "timeout"])
async def test_real_service_build_path_failure_is_a_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    from collections.abc import Iterable, Sequence

    from mlops_agents.sandbox import images
    from mlops_agents.sandbox import service as service_module
    from mlops_agents.sandbox.docker import DockerResult
    from mlops_agents.sandbox.service import SandboxService
    from tests.sandbox.fakes import MemorySandboxProjects
    from tests.sandbox.test_service_env import FakeEventLog
    from tests.sandbox.test_service_run import FakeRunDocker

    class FailingBuild(FakeRunDocker):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "build":
                if phase == "timeout":
                    raise TimeoutError("build timed out")
                raise OSError("build I/O failure")
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError(f"{phase} denied")

    if phase == "snapshot":
        monkeypatch.setattr(service_module, "snapshot_image", fail)
    elif phase == "context":
        monkeypatch.setattr(images, "create_build_context", fail)
    events = FakeEventLog()
    service = SandboxService(FailingBuild(), projects=MemorySandboxProjects(), events=events)
    task = TaskRef("tsk_build_failure", "Failure", "branch", tmp_path, "active")
    files = MockFileAccess(tmp_path)
    result = await BashTool(service).invoke(make_context(tmp_path, task, files), {"command": "ls"})
    assert result.is_error
    assert "Environment build failed" in result.content
    assert files.invalidated
    assert events.records[-1].type == "sandbox.build.failed"
    assert events.records[-1].payload["detail"]


@pytest.mark.parametrize("operation", ["inspect", "rm", "run", "exec"])
@pytest.mark.parametrize("error", [TimeoutError("operation timed out"), OSError("I/O failed")])
async def test_container_operational_failure_becomes_tool_error(
    tmp_path: Path,
    operation: str,
    error: OSError,
) -> None:
    from collections.abc import Iterable, Sequence

    from mlops_agents.sandbox.docker import DockerResult
    from mlops_agents.sandbox.service import SandboxService
    from tests.sandbox.fakes import MemorySandboxProjects
    from tests.sandbox.test_service_run import FakeRunDocker

    class FailedOperation(FakeRunDocker):
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

    service = SandboxService(FailedOperation(), projects=MemorySandboxProjects())
    task = TaskRef("tsk_container_failure", "Failure", "branch", tmp_path, "active")
    result = await BashTool(service).invoke(make_context(tmp_path, task), {"command": "ls"})
    if operation == "exec" and isinstance(error, TimeoutError):
        assert not result.is_error
        assert "was stopped" in result.content
    else:
        assert result.is_error
        assert "sandbox" in result.content.lower()
