from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

import pytest

from mlops_agents.sandbox.contract import DockerStatus
from mlops_agents.sandbox.docker import CliDockerRunner, DockerResult, DockerRunner, detect_docker
from mlops_agents.sandbox.service import SandboxService


class FakeDockerRunner:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        raise_not_found: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.raise_not_found = raise_not_found
        self.calls: list[tuple[Sequence[str], float | None, tuple[str, ...]]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        self.calls.append((args, timeout_s, tuple(secrets)))
        if self.raise_not_found:
            raise FileNotFoundError("docker not found")
        return DockerResult(stdout=self.stdout, stderr=self.stderr, exit_code=self.exit_code)


async def test_detect_docker_available() -> None:
    version_info = {
        "Client": {"Version": "27.3.1"},
        "Server": {"Version": "27.3.1"},
    }
    runner = FakeDockerRunner(stdout=json.dumps(version_info), exit_code=0)
    status = await detect_docker(runner)
    assert status == DockerStatus(available=True, version="27.3.1", detail=None)
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == ["version", "--format", "json"]


async def test_detect_docker_not_installed() -> None:
    runner = FakeDockerRunner(raise_not_found=True)
    status = await detect_docker(runner)
    assert status.available is False
    assert status.version is None
    assert status.detail == "Docker is not installed or not on PATH."


async def test_detect_docker_daemon_down_with_client_version() -> None:
    version_info = {"Client": {"Version": "27.3.1"}}
    runner = FakeDockerRunner(
        stdout=json.dumps(version_info),
        stderr="Cannot connect to the Docker daemon",
        exit_code=1,
    )
    status = await detect_docker(runner)
    assert status.available is False
    assert status.version == "27.3.1"
    assert status.detail == "Docker daemon is not running."


async def test_detect_docker_daemon_down_unparseable_output() -> None:
    runner = FakeDockerRunner(
        stdout="not json",
        stderr="Cannot connect to the Docker daemon",
        exit_code=1,
    )
    status = await detect_docker(runner)
    assert status.available is False
    assert status.version is None
    assert status.detail == "Docker daemon is not running."


async def test_cli_runner_scrubs_output_and_secrets() -> None:
    import sys

    code = "import sys; sys.stdout.write('secret-123 and https://user:pass@host/api?token=abc'); sys.stderr.write('err secret-123')"
    runner = CliDockerRunner(executable=sys.executable)
    assert isinstance(runner, DockerRunner)

    res = await runner.run(["-c", code], secrets=["secret-123"])
    assert res.exit_code == 0
    assert "secret-123" not in res.stdout
    assert "user:pass@" not in res.stdout
    assert "token=abc" not in res.stdout
    assert "***" in res.stdout
    assert "secret-123" not in res.stderr
    assert "***" in res.stderr


async def test_cli_runner_replaces_invalid_utf8() -> None:
    import sys

    code = "import sys; sys.stdout.buffer.write(b'hello \\xff\\xfe world')"
    runner = CliDockerRunner(executable=sys.executable)
    res = await runner.run(["-c", code])
    assert res.exit_code == 0
    assert "hello" in res.stdout
    assert "\ufffd" in res.stdout


async def test_cli_runner_timeout() -> None:
    import sys

    code = "import time; time.sleep(5)"
    runner = CliDockerRunner(executable=sys.executable)
    with pytest.raises(TimeoutError):
        await runner.run(["-c", code], timeout_s=0.1)


@pytest.mark.docker
async def test_real_docker_available() -> None:
    service = SandboxService()
    status = await service.docker()
    assert status.available is True
    assert status.version is not None
    assert status.detail is None
