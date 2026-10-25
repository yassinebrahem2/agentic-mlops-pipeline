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

DOCKER_INSTALL_MESSAGE = (
    "Docker is not installed or not on PATH. Install Docker Desktop "
    "(https://www.docker.com/products/docker-desktop) or Docker Engine to use the Bash sandbox."
)

HEAD_CAPTURE_CHARS = 15_000
TAIL_CAPTURE_CHARS = 15_000


class DockerTimeoutError(TimeoutError):
    """Raised when a docker command exceeds its timeout, preserving captured streams."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class StreamCapture:
    """Incrementally captures at most head and tail characters without unbounded buffering."""

    def __init__(
        self,
        head_limit: int = HEAD_CAPTURE_CHARS,
        tail_limit: int = TAIL_CAPTURE_CHARS,
    ) -> None:
        self.head_limit = head_limit
        self.tail_limit = tail_limit
        self.head = ""
        self.tail = ""
        self.total_chars = 0

    def feed(self, text: str) -> None:
        if not text:
            return
        self.total_chars += len(text)
        if len(self.head) < self.head_limit:
            needed = self.head_limit - len(self.head)
            self.head += text[:needed]
            remaining = text[needed:]
        else:
            remaining = text
        if remaining:
            self.tail = (self.tail + remaining)[-self.tail_limit :]

    def get_output(self) -> str:
        if self.total_chars <= self.head_limit:
            return self.head
        if self.total_chars <= self.head_limit + self.tail_limit:
            return self.head + self.tail
        cut_count = self.total_chars - (len(self.head) + len(self.tail))
        cut_note = f"\n\n[... {cut_count} characters truncated ...]\n\n"
        return f"{self.head}{cut_note}{self.tail}"


async def _consume_stream(
    reader: asyncio.StreamReader | None,
    capture: StreamCapture,
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        capture.feed(text)


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
    """Runs the docker CLI without a shell, decoding, bounding, and scrubbing output."""

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

        stdout_cap = StreamCapture()
        stderr_cap = StreamCapture()

        async def stream_and_wait() -> int:
            await asyncio.gather(
                _consume_stream(proc.stdout, stdout_cap),
                _consume_stream(proc.stderr, stderr_cap),
            )
            return await proc.wait()

        try:
            if timeout_s is not None:
                returncode = await asyncio.wait_for(stream_and_wait(), timeout=timeout_s)
            else:
                returncode = await stream_and_wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            raw_stdout = scrub_values(scrub_url_secrets(stdout_cap.get_output()), secrets)
            raw_stderr = scrub_values(scrub_url_secrets(stderr_cap.get_output()), secrets)
            raise DockerTimeoutError(
                f"Docker command timed out after {timeout_s}s",
                stdout=raw_stdout,
                stderr=raw_stderr,
            ) from None

        raw_stdout = scrub_values(scrub_url_secrets(stdout_cap.get_output()), secrets)
        raw_stderr = scrub_values(scrub_url_secrets(stderr_cap.get_output()), secrets)
        return DockerResult(
            stdout=raw_stdout,
            stderr=raw_stderr,
            exit_code=returncode,
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
        result = await runner.run(["version", "--format", "json"], timeout_s=10.0)
    except FileNotFoundError:
        return DockerStatus(
            available=False,
            version=None,
            detail=DOCKER_INSTALL_MESSAGE,
        )
    except TimeoutError:
        return DockerStatus(
            available=False,
            version=None,
            detail="Docker command timed out checking version.",
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
            detail="Docker daemon is not running. Please start Docker Desktop or the Docker daemon.",
        )

    return DockerStatus(
        available=True,
        version=server_version or client_version,
        detail=None,
    )
