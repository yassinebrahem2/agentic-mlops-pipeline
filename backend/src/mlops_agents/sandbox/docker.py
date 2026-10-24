"""The one place that runs the docker CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.sandbox.contract import DockerStatus


@dataclass(frozen=True)
class DockerResult:
    stdout: str
    stderr: str
    exit_code: int


@runtime_checkable
class DockerRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult: ...


class CliDockerRunner:
    """Runs the docker CLI without a shell, decoding and scrubbing output."""

    def __init__(self, executable: str = "docker") -> None:
        self._executable = executable

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise

        try:
            if timeout_s is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            else:
                stdout_bytes, stderr_bytes = await proc.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout = scrub_values(scrub_url_secrets(stdout), secrets)
        stderr = scrub_values(scrub_url_secrets(stderr), secrets)
        return DockerResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode if proc.returncode is not None else -1,
        )


def _extract_version(container: object) -> str | None:
    if not isinstance(container, dict):
        return None
    d = cast(dict[object, object], container)
    ver = d.get("Version")
    return ver if isinstance(ver, str) else None


async def detect_docker(runner: DockerRunner) -> DockerStatus:
    """Detects Docker availability and version via `docker version --format json`."""
    try:
        result = await runner.run(["version", "--format", "json"])
    except FileNotFoundError:
        return DockerStatus(
            available=False,
            version=None,
            detail="Docker is not installed or not on PATH.",
        )

    client_version: str | None = None
    server_version: str | None = None
    with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError, ValueError):
        loaded: object = json.loads(result.stdout)
        if isinstance(loaded, dict):
            d = cast(dict[object, object], loaded)
            client_version = _extract_version(d.get("Client"))
            server_version = _extract_version(d.get("Server"))

    if result.exit_code != 0 or server_version is None:
        return DockerStatus(
            available=False,
            version=client_version,
            detail="Docker daemon is not running.",
        )

    return DockerStatus(
        available=True,
        version=server_version or client_version,
        detail=None,
    )
