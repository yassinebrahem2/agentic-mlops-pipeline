"""Tests for CommandRunner in monitoring/hooks/command_runner.py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.credentials.project_secrets import hook_ref
from mlops_agents.monitoring.hooks.command_runner import CommandRunner
from mlops_agents.monitoring.hooks.constants import HOOK_CREDENTIAL_ENV, HOOK_DETAIL_MAX_CHARS
from mlops_agents.monitoring.hooks.contract import (
    HookRefused,
    HookView,
)
from mlops_agents.proposals.payloads import CommandSpec, Placeholder
from mlops_agents.sandbox.contract import (
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
    SandboxUnavailable,
)
from mlops_agents.sandbox.docker import DockerResult, DockerTimeoutError
from tests.conftest import MemoryKeyStore
from tests.sandbox.fakes import FakeDockerRunner, MemorySandboxProjects


class HookFakeDockerRunner(FakeDockerRunner):
    """Extended FakeDockerRunner that can intercept call to inspect the env-file on disk."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.env_file_checked_exists: bool | None = None
        self.env_file_contents: str | None = None
        self.custom_run_result: DockerResult | None = None
        self.run_raises: BaseException | None = None

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        if cmd and cmd[0] == "run":
            if "--env-file" in cmd:
                env_path_str = cmd[cmd.index("--env-file") + 1]
                p = Path(env_path_str)
                self.env_file_checked_exists = await asyncio.to_thread(p.exists)
                if self.env_file_checked_exists:
                    self.env_file_contents = await asyncio.to_thread(p.read_text, encoding="utf-8")

            if self.run_raises is not None:
                self.calls.append(cmd)
                self.timeouts.append(timeout_s)
                raise self.run_raises

            if self.custom_run_result is not None:
                self.calls.append(cmd)
                self.timeouts.append(timeout_s)
                return self.custom_run_result

        return await super().run(args, timeout_s=timeout_s, secrets=secrets)


class FakeSandbox(Sandbox):
    def __init__(
        self,
        docker_available: bool = True,
        rebuild_tag: str = "app-env:testtag",
        rebuild_state: str = "ready",
        rebuild_exc: Exception | None = None,
    ) -> None:
        self.docker_available = docker_available
        self.rebuild_tag = rebuild_tag
        self.rebuild_state = rebuild_state
        self.rebuild_exc = rebuild_exc
        self.rebuild_calls: list[str] = []

    async def docker(self) -> DockerStatus:
        return DockerStatus(
            available=self.docker_available,
            version="27.0.0" if self.docker_available else None,
            detail=None if self.docker_available else "Docker daemon is not running",
        )

    async def environment(self, project_id: str) -> EnvironmentStatus:
        return EnvironmentStatus(state="ready", tag=self.rebuild_tag, detail=None)

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        self.rebuild_calls.append(project_id)
        if not self.docker_available:
            raise SandboxUnavailable("Docker is not available.")
        if self.rebuild_exc is not None:
            raise self.rebuild_exc
        return EnvironmentStatus(
            state=self.rebuild_state,  # type: ignore[arg-type]
            tag=self.rebuild_tag,
            detail=None,
        )

    async def run(self, project_id: str, task: Any, command: str, timeout_s: int) -> Any:
        raise NotImplementedError

    async def remove_task(self, project_id: str, task_id: str) -> None:
        pass

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None, None]:
        yield

    async def close(self) -> None:
        pass


def make_command_hook(
    hook_id: str = "hok_test1",
    template: str = "echo {model}",
    placeholders: list[Placeholder] | None = None,
) -> HookView:
    return HookView(
        id=hook_id,
        name="test-hook",
        model="iris",
        spec=CommandSpec(
            kind="train",
            type="command",
            template=template,
            workflow=None,
            inputs={},
            placeholders=placeholders if placeholders is not None else ["model"],
            deploys=False,
            entry_files=[],
        ),
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(),
    )


@pytest.mark.asyncio
async def test_command_runner_success_with_credential_and_env_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    conn_data = tmp_path / "conn"
    conn_data.mkdir()

    class DummyMount:
        host_path = conn_data
        container_path = "/data/myconn"

    projects = MemorySandboxProjects(
        roots={"prj_1": repo_root},
        mounts={"prj_1": [DummyMount()]},  # type: ignore[list-item]
    )
    keystore = MemoryKeyStore()
    secret_val = "super-secret-token-xyz"
    keystore.set(hook_ref("prj_1", "hok_1"), secret_val)

    docker_runner = HookFakeDockerRunner()
    sandbox = FakeSandbox(rebuild_tag="app-env:readytag")

    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=keystore,
    )

    hook = make_command_hook(hook_id="hok_1", template="echo {model}")
    authority = Authority(basis="approval", ref="apr_123", reason="test")
    params = {"model": "iris-v1", "request_id": "req_abc"}

    res = await runner.run("prj_1", hook, params, authority)

    assert res.ok is True
    assert res.request_id == "req_abc"
    assert res.ci_run_url is None

    # Check docker calls
    assert len(docker_runner.calls) == 1
    call_args = docker_runner.calls[0]

    # Flags present
    assert call_args[0] == "run"
    assert "--rm" in call_args
    assert "--name" in call_args
    name_idx = call_args.index("--name")
    assert call_args[name_idx + 1].startswith("hook-")
    assert "app-env:readytag" in call_args
    assert "--network" not in call_args  # network enabled by default
    assert "--env-file" in call_args

    # Check mounts
    assert any("target=/workspace,readonly" in a for a in call_args)
    assert any("target=/data/myconn,readonly" in a for a in call_args)

    # Credential never appears in CLI args
    assert secret_val not in " ".join(call_args)

    # Env file existed during call and contained HOOK_CREDENTIAL_ENV=<value>
    assert docker_runner.env_file_checked_exists is True
    assert docker_runner.env_file_contents == f"{HOOK_CREDENTIAL_ENV}={secret_val}\n"

    # Env file is deleted after run
    env_file_path_str = call_args[call_args.index("--env-file") + 1]
    assert not await asyncio.to_thread(Path(env_file_path_str).exists)


@pytest.mark.asyncio
async def test_command_runner_no_credential_gets_no_env_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    keystore = MemoryKeyStore()  # empty

    docker_runner = HookFakeDockerRunner()
    sandbox = FakeSandbox()

    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=keystore,
    )

    hook = make_command_hook(hook_id="hok_nocred", template="echo {model}")
    res = await runner.run("prj_1", hook, {"model": "iris"}, Authority(basis="none", reason="test"))
    assert res.ok is True

    call_args = docker_runner.calls[0]
    assert "--env-file" not in call_args


@pytest.mark.asyncio
async def test_command_runner_env_file_deleted_on_timeout_and_error(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    keystore = MemoryKeyStore()
    keystore.set(hook_ref("prj_1", "hok_timeout"), "secret123")

    # Case 1: DockerTimeoutError
    docker_runner = HookFakeDockerRunner()
    docker_runner.run_raises = DockerTimeoutError(
        "Timed out", stdout="output before timeout", stderr="stderr info"
    )
    sandbox = FakeSandbox()
    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=keystore,
    )

    hook = make_command_hook(hook_id="hok_timeout", template="echo {model}")
    res = await runner.run("prj_1", hook, {"model": "test"}, Authority(basis="none", reason="test"))
    assert res.ok is False
    assert "timeout" in res.detail.lower()

    # Env file existed during execution and is deleted after timeout
    assert docker_runner.env_file_checked_exists is True
    # The last call had an env-file
    last_call = docker_runner.calls[0]
    env_path_str = last_call[last_call.index("--env-file") + 1]
    assert not await asyncio.to_thread(Path(env_path_str).exists)

    # Case 2: Generic Exception
    docker_runner2 = HookFakeDockerRunner()
    docker_runner2.run_raises = RuntimeError("unexpected process crash")
    runner2 = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner2,
        keystore=keystore,
    )
    with pytest.raises(RuntimeError):
        await runner2.run("prj_1", hook, {"model": "test"}, Authority(basis="none", reason="test"))

    assert docker_runner2.env_file_checked_exists is True
    last_call2 = docker_runner2.calls[0]
    env_path_str2 = last_call2[last_call2.index("--env-file") + 1]
    assert not await asyncio.to_thread(Path(env_path_str2).exists)


@pytest.mark.asyncio
async def test_command_runner_quotes_param_values(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    docker_runner = HookFakeDockerRunner()
    sandbox = FakeSandbox()
    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook(
        template="python train.py --version {candidate_version}",
        placeholders=["candidate_version"],
    )
    dangerous_value = "x; rm -rf /"
    res = await runner.run(
        "prj_1",
        hook,
        {"candidate_version": dangerous_value},
        Authority(basis="none", reason="test"),
    )
    assert res.ok is True

    call_args = docker_runner.calls[0]
    # The bash -lc argument should have quoted the value safely
    bash_cmd = call_args[call_args.index("-lc") + 1]
    assert "python train.py --version 'x; rm -rf /'" in bash_cmd


@pytest.mark.asyncio
async def test_command_runner_missing_placeholder_refused_no_docker(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    docker_runner = HookFakeDockerRunner()
    sandbox = FakeSandbox()
    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook(
        template="echo {model} {candidate_version}",
        placeholders=["model", "candidate_version"],
    )
    with pytest.raises(HookRefused) as exc_info:
        await runner.run("prj_1", hook, {"model": "iris"}, Authority(basis="none", reason="test"))
    assert "candidate_version" in str(exc_info.value)
    # Docker was never called!
    assert len(docker_runner.calls) == 0
    assert len(sandbox.rebuild_calls) == 0


@pytest.mark.asyncio
async def test_command_runner_scrubs_credential_and_caps_detail(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    keystore = MemoryKeyStore()
    secret = "secret-token-to-scrub-999"
    keystore.set(hook_ref("prj_1", "hok_scrub"), secret)

    long_prefix = "A" * (HOOK_DETAIL_MAX_CHARS + 500)
    fake_output = f"{long_prefix} leaked: {secret} and tail end"

    docker_runner = HookFakeDockerRunner()
    docker_runner.custom_run_result = DockerResult(
        stdout=fake_output,
        stderr="",
        exit_code=1,
    )
    sandbox = FakeSandbox()
    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=keystore,
    )

    hook = make_command_hook(hook_id="hok_scrub", template="echo {model}")
    res = await runner.run("prj_1", hook, {"model": "iris"}, Authority(basis="none", reason="test"))

    assert res.ok is False
    # Secret must be scrubbed
    assert secret not in res.detail
    assert "[REDACTED]" in res.detail or "***" in res.detail
    # Detail is capped at HOOK_DETAIL_MAX_CHARS
    assert len(res.detail) <= HOOK_DETAIL_MAX_CHARS
    # Detail is the tail end
    assert "tail end" in res.detail


@pytest.mark.asyncio
async def test_command_runner_docker_unavailable(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    docker_runner = HookFakeDockerRunner()
    sandbox = FakeSandbox(docker_available=False)

    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook()
    res = await runner.run("prj_1", hook, {"model": "iris"}, Authority(basis="none", reason="test"))

    assert res.ok is False
    assert "Docker is not available" in res.detail


@pytest.mark.asyncio
async def test_command_runner_removes_container_on_timeout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    docker_runner = HookFakeDockerRunner()
    docker_runner.run_raises = DockerTimeoutError("Command timed out", output="timed out")
    sandbox = FakeSandbox()

    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook(hook_id="hok_timeout", template="sleep 100")
    res = await runner.run("prj_1", hook, {}, Authority(basis="none", reason="test"))

    assert res.ok is False
    assert "timed out" in res.detail
    call_args = docker_runner.calls[0]
    assert "--name" in call_args
    name_idx = call_args.index("--name")
    container_name = call_args[name_idx + 1]
    assert container_name.startswith("hook-")
    # Check that docker rm -f was called with that exact container name
    rm_calls = [c for c in docker_runner.calls if c[:3] == ["rm", "-f", container_name]]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_command_runner_removes_container_on_cancel(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})
    docker_runner = HookFakeDockerRunner()
    docker_runner.run_raises = asyncio.CancelledError()
    sandbox = FakeSandbox()

    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook(hook_id="hok_cancel", template="sleep 100")
    with pytest.raises(asyncio.CancelledError):
        await runner.run("prj_1", hook, {}, Authority(basis="none", reason="test"))

    call_args = docker_runner.calls[0]
    assert "--name" in call_args
    name_idx = call_args.index("--name")
    container_name = call_args[name_idx + 1]
    assert container_name.startswith("hook-")
    # Check that docker rm -f was called with that exact container name
    rm_calls = [c for c in docker_runner.calls if c[:3] == ["rm", "-f", container_name]]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_command_runner_rm_failure_logged_on_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    projects = MemorySandboxProjects(roots={"prj_1": repo_root})

    class FailingRmDockerRunner(HookFakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            cmd = list(args)
            if cmd and cmd[0] == "run":
                self.calls.append(cmd)
                raise DockerTimeoutError("Command timed out", output="timed out")
            if cmd and cmd[:2] == ["rm", "-f"]:
                self.calls.append(cmd)
                raise RuntimeError("Docker daemon disconnected during rm")
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    docker_runner = FailingRmDockerRunner()
    sandbox = FakeSandbox()
    runner = CommandRunner(
        sandbox=sandbox,
        projects=projects,
        docker_runner=docker_runner,
        keystore=MemoryKeyStore(),
    )

    hook = make_command_hook(hook_id="hok_rm_fail", template="sleep 100")
    import logging

    with caplog.at_level(logging.ERROR):
        res = await runner.run("prj_1", hook, {}, Authority(basis="none", reason="test"))

    assert res.ok is False
    assert "timed out" in res.detail
    assert "Failed to remove timed-out hook container" in caplog.text
