"""Code parser runner in isolated sandbox containers (I22)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, cast

from mlops_agents.connections.contract import Mount
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.ids import new_id
from mlops_agents.monitoring.results.contract import (
    ParseOutcome,
    ParserCode,
    ResultRecord,
)
from mlops_agents.sandbox.containers import build_one_shot_run_args
from mlops_agents.sandbox.contract import EnvironmentStatus
from mlops_agents.sandbox.docker import DockerResult, DockerRunner, DockerTimeoutError

logger = logging.getLogger(__name__)

# The team's script sees only the chunk it is given, on stdin: DockerRunner has no stdin, so
# this stdlib-only bootstrap reads the chunk's bytes from the read-only mount and runs the
# script over them. Argv: file path, start byte offset, byte length, script, origin_ref.
# What the script itself receives is CODE_PARSER_CONTRACT in parser_kind.py. A chunk that
# does not start the file lacks its header line, so the bootstrap reads that line here
# (bounded) and passes it on.
_BOOTSTRAP = """\
import io, sys
path, start, length, script, origin_ref = sys.argv[1:6]
first_line = ''
with open(path, 'rb') as f:
    if int(start) > 0:
        first_line = f.readline(65536).rstrip(b'\\r\\n').decode('utf-8', errors='replace')
    f.seek(int(start))
    data = f.read(int(length))
sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding='utf-8', errors='replace')
sys.argv = ['<parser>', origin_ref, start, first_line]
exec(compile(script, '<parser>', 'exec'), {'__name__': '__main__'})
"""


class _Connections(Protocol):
    async def mounts(self, project_id: str) -> list[Mount]: ...


class _Sandbox(Protocol):
    async def rebuild(self, project_id: str) -> EnvironmentStatus: ...


@dataclass(frozen=True)
class _Prepared:
    """What preparing a project's sandbox gives every run: its image and the data mounts."""

    image_tag: str
    mounts: list[Mount]


@dataclass
class _Cycle:
    """One collection cycle's preparation for `project_id` by `runner`: done on its first run,
    kept until the cycle ends, a failure included."""

    runner: CodeParserRunner
    project_id: str
    prepared: _Prepared | None = None
    failure: str | None = None


# Task-local, so concurrent collections of other projects and direct runs prepare their own
_current_cycle: ContextVar[_Cycle | None] = ContextVar("code_parser_cycle", default=None)


@dataclass(frozen=True)
class _Target:
    """What every run over one chunk shares."""

    project_id: str
    image_tag: str
    mounts: list[Mount]
    code: ParserCode
    container_path: str
    origin_ref: str


class CodeParserRunner:
    """Executes a ParserCode script inside an isolated sandbox container."""

    def __init__(
        self,
        sandbox: _Sandbox,
        docker_runner: DockerRunner,
        connections: _Connections,
    ) -> None:
        self._sandbox = sandbox
        self._docker_runner = docker_runner
        self._connections = connections

    @asynccontextmanager
    async def cycle(self, project_id: str) -> AsyncGenerator[None]:
        """A collection cycle of `project_id`: its runs share one sandbox rebuild and mounts
        lookup, done on the first run, so a cycle without a code parser run does neither. A
        failed preparation fails every later run of the cycle without being retried."""
        token = _current_cycle.set(_Cycle(runner=self, project_id=project_id))
        try:
            yield
        finally:
            _current_cycle.reset(token)

    async def run(
        self,
        project_id: str,
        code: ParserCode,
        raw: str,
        origin_ref: str,
        *,
        connection_name: str | None = None,
        start_offset: int = 0,
    ) -> ParseOutcome:
        if connection_name is None:
            raise RuntimeError(f"Code parser has no connection to read {origin_ref!r} from")

        prepared = await self._prepared(project_id)
        target = _Target(
            project_id=project_id,
            image_tag=prepared.image_tag,
            mounts=prepared.mounts,
            code=code,
            container_path=f"{DATA_MOUNT_ROOT}/{connection_name}/{origin_ref}",
            origin_ref=origin_ref,
        )
        # The chunk's bytes: exact, since the reader decodes a code parser's chunk with
        # surrogate escapes, so bytes that are not UTF-8 encode back to themselves.
        data = raw.encode("utf-8", errors="surrogateescape")

        records: list[tuple[str, ResultRecord]] = []
        errors: list[str] = []
        for piece_start, stdout in await self._run_split(target, start_offset, data):
            outcome = _parse_stdout(stdout, origin_ref, piece_start)
            records.extend(outcome.records)
            errors.extend(outcome.errors)
        return ParseOutcome(records=records, errors=errors)

    async def _prepared(self, project_id: str) -> _Prepared:
        """The project's preparation: the current cycle's when this run is in one."""
        cycle = _current_cycle.get()
        if cycle is None or cycle.runner is not self or cycle.project_id != project_id:
            return await self._prepare(project_id)
        if cycle.failure is not None:
            raise RuntimeError(cycle.failure)
        if cycle.prepared is None:
            try:
                cycle.prepared = await self._prepare(project_id)
            except Exception as exc:
                cycle.failure = str(exc)
                raise
        return cycle.prepared

    async def _prepare(self, project_id: str) -> _Prepared:
        env_status = await self._sandbox.rebuild(project_id)
        if env_status.tag is None or env_status.state != "ready":
            raise RuntimeError(
                f"Sandbox unavailable for project {project_id}: {env_status.detail or env_status.state}"
            )
        return _Prepared(
            image_tag=env_status.tag, mounts=await self._connections.mounts(project_id)
        )

    async def _run_split(
        self, target: _Target, start_offset: int, data: bytes
    ) -> list[tuple[int, str]]:
        """Each run's start offset and stdout for `data`, a chunk's bytes from `start_offset`.

        A run whose output the capture cut is run again as two pieces split at the line start
        nearest its middle, down to a single line: the script decides how much it prints."""
        res = await self._run_once(target, start_offset, len(data))
        # Records come from stdout alone: the combined `output` interleaves stderr, so a
        # warning line would be read as bad JSON. `truncated` flags the combined capture.
        if not res.truncated:
            return [(start_offset, res.stdout)]
        cut = _middle_line_start(data)
        if cut is None:
            raise RuntimeError("Code parser output was truncated")
        first = await self._run_split(target, start_offset, data[:cut])
        second = await self._run_split(target, start_offset + cut, data[cut:])
        return [*first, *second]

    async def _run_once(self, target: _Target, start_offset: int, byte_length: int) -> DockerResult:
        container_name = f"parser-{new_id('res')}"
        command = [
            "python",
            "-c",
            _BOOTSTRAP,
            target.container_path,
            str(start_offset),
            str(byte_length),
            target.code.script,
            target.origin_ref,
        ]

        run_args = build_one_shot_run_args(
            project_id=target.project_id,
            image_tag=target.image_tag,
            command=command,
            network=False,
            mounts=target.mounts,
            env_file=None,
            name=container_name,
        )

        try:
            res = await self._docker_runner.run(run_args, timeout_s=float(target.code.timeout_s))
        except DockerTimeoutError as err:
            try:
                await self._docker_runner.run(["rm", "-f", container_name], timeout_s=15.0)
            except Exception:
                logger.exception("Failed to remove timed-out parser container %s", container_name)
            raise RuntimeError(f"Code parser execution timed out: {err}") from err
        except asyncio.CancelledError:
            try:
                await self._docker_runner.run(["rm", "-f", container_name], timeout_s=15.0)
            except Exception:
                logger.exception("Failed to remove cancelled parser container %s", container_name)
            raise

        if res.exit_code != 0:
            output, _truncated = res.command_output()
            err_msg = res.stderr or output or f"Container exited with code {res.exit_code}"
            raise RuntimeError(f"Code parser execution failed: {err_msg}")
        return res


def _middle_line_start(data: bytes) -> int | None:
    """The line start in `data` nearest its middle, other than its first line's; None for a
    single line."""
    middle = len(data) // 2
    before = data.rfind(b"\n", 0, middle)
    after = data.find(b"\n", middle, len(data) - 1)
    cuts = [newline + 1 for newline in (before, after) if newline != -1]
    return min(cuts, key=lambda cut: abs(cut - middle)) if cuts else None


def _parse_stdout(stdout: str, origin_ref: str, start_offset: int) -> ParseOutcome:
    """The records a run printed, keyed by its start offset when they carry no key."""
    records: list[tuple[str, ResultRecord]] = []
    errors: list[str] = []

    stdout_trimmed = stdout.strip()
    if not stdout_trimmed:
        return ParseOutcome(records=[], errors=[])

    # Try parsing as JSON array first, else JSON Lines
    items: list[dict[str, Any]] = []
    try:
        parsed: object = json.loads(stdout_trimmed)
        if isinstance(parsed, list):
            parsed_list = cast(list[object], parsed)
            for it in parsed_list:
                if isinstance(it, dict):
                    items.append(cast(dict[str, Any], it))
        elif isinstance(parsed, dict):
            items.append(cast(dict[str, Any], parsed))
    except Exception:
        for line in stdout_trimmed.splitlines():
            l_str = line.strip()
            if not l_str:
                continue
            try:
                p: object = json.loads(l_str)
                if isinstance(p, dict):
                    items.append(cast(dict[str, Any], p))
                else:
                    errors.append(f"Line is not a dict: {l_str}")
            except Exception as exc:
                errors.append(f"Invalid JSON line: {exc}")

    for idx, item in enumerate(items):
        try:
            item_dict = dict(item)
            item_dict["origin"] = "team"
            if not item_dict.get("source_ref"):
                item_dict["source_ref"] = origin_ref
            if "detail" not in item_dict or not isinstance(item_dict["detail"], dict):
                item_dict["detail"] = {}
            custom_key = item_dict.pop("source_key", None) or item_dict.pop("key", None)
            # Distinct per chunk start, and the same when a failed chunk is read again
            source_key = (
                str(custom_key) if custom_key is not None else f"{origin_ref}:{start_offset}:{idx}"
            )
            rec = ResultRecord.model_validate(item_dict)
            records.append((source_key, rec))
        except Exception as exc:
            errors.append(f"Record {idx}: {exc}")

    return ParseOutcome(records=records, errors=errors)
