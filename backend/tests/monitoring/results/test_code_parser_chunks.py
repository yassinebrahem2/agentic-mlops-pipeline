"""A code parser reads a file in bounded pieces, each its own run, so no run's output exceeds
the sandbox's capture (I22)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from sqlalchemy import select

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.constants import (
    CODE_PARSER_CHUNK_BYTES,
    HELD_TAIL_QUIET_SECONDS,
)
from mlops_agents.monitoring.results.contract import source_id
from mlops_agents.monitoring.results.file_source import FileSourceReader
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.sandbox.docker import HEAD_CAPTURE_CHARS, TAIL_CAPTURE_CHARS
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import results
from tests.monitoring.results.conftest import (
    FakeConnections,
    FakeIncidents,
    LocalExecDockerRunner,
    code_feed_service,
    set_mtime,
)
from tests.providers.fake_clock import FakeClock

_SOURCE = "file:conn_feed:feed.raw"
_LINE_BYTES = 100
# Reads only its stdin and emits one record per non-empty line, its check_id the line's first
# field. It exits with an error on a line holding BOOM while the flag file exists.
_SCRIPT = """
import json, os, sys
FLAG = __FLAG__
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    if "BOOM" in line and FLAG and os.path.exists(FLAG):
        raise SystemExit("marker line while the flag file exists")
    print(json.dumps({
        "check_id": line.split(",")[0],
        "ran_at": "2026-10-30T10:00:00Z",
        "fired": False,
        "category": "data_quality",
        "subject": sys.argv[1],
        "detail": {},
    }))
"""


def _script(flag: Path | None = None) -> str:
    return _SCRIPT.replace("__FLAG__", repr(str(flag) if flag is not None else ""))


def _line(i: int, marker: str = "") -> bytes:
    return f"c{i:04d},{marker}".ljust(_LINE_BYTES - 1, "x").encode() + b"\n"


def _runs(runner: LocalExecDockerRunner) -> list[tuple[int, int]]:
    """(start offset, byte length) of each parser run, from the bootstrap's arguments."""
    runs: list[tuple[int, int]] = []
    for call in runner.calls:
        command = call[call.index("sandbox:test-tag") + 1 :]
        runs.append((int(command[4]), int(command[5])))
    return runs


def _assert_line_pieces(data: bytes, runs: list[tuple[int, int]]) -> None:
    """The runs cover `data` in order, each at most the bound and starting at a line start."""
    assert runs[0][0] == 0
    for (start, length), (next_start, _next_length) in pairwise(runs):
        assert next_start == start + length
    for start, length in runs:
        assert length <= CODE_PARSER_CHUNK_BYTES
        assert start == 0 or data[start - 1 : start] == b"\n"
    assert sum(length for _start, length in runs) == len(data)


async def _check_ids(databases: Databases, project_id: str) -> list[str]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(results))).mappings().all()
    return [r["check_id"] for r in rows]


async def _offset(service: ResultsService, project_id: str) -> int:
    state = await service.store.get_source_state(project_id, _SOURCE)
    assert state is not None
    return state.cursor["files"]["feed.raw"]["offset"]


async def test_file_whose_records_exceed_one_capture_is_read_in_pieces(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    data = b"".join(_line(i) for i in range(1_000))
    (data_dir / "feed.raw").write_bytes(data)
    service, runner = await code_feed_service(
        databases, events, project_id, data_dir, "feed.raw", _script()
    )

    rep = await service.collect(project_id, clock.now())

    assert rep.failures == {}
    assert rep.stored == 1_000
    check_ids = await _check_ids(databases, project_id)
    assert sorted(check_ids) == [f"c{i:04d}" for i in range(1_000)]
    assert await _offset(service, project_id) == len(data)
    runs = _runs(runner)
    assert len(runs) > 1
    _assert_line_pieces(data, runs)
    # The file's records in one run would exceed the capture
    record: dict[str, object] = {
        "check_id": "c0000",
        "ran_at": "2026-10-30T10:00:00Z",
        "fired": False,
        "category": "data_quality",
        "subject": "feed.raw",
        "detail": {},
    }
    assert 1_000 * len(json.dumps(record)) > HEAD_CAPTURE_CHARS + TAIL_CAPTURE_CHARS


async def test_failed_middle_piece_keeps_the_cursor_at_its_start(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    flag = tmp_path / "fail.flag"
    flag.write_bytes(b"")
    lines = 3 * (CODE_PARSER_CHUNK_BYTES // _LINE_BYTES) - 3
    marker_at = lines // 2
    data = b"".join(_line(i, "BOOM" if i == marker_at else "") for i in range(lines))
    (data_dir / "feed.raw").write_bytes(data)
    incidents = FakeIncidents()
    service, runner = await code_feed_service(
        databases, events, project_id, data_dir, "feed.raw", _script(flag), incidents
    )

    rep1 = await service.collect(project_id, clock.now())

    # The first piece is stored, the second fails, the third is not run this cycle
    runs1 = _runs(runner)
    assert len(runs1) == 2
    second_start = runs1[1][0]
    first_lines = second_start // _LINE_BYTES
    assert 0 < first_lines <= marker_at < first_lines + runs1[1][1] // _LINE_BYTES
    assert _SOURCE in rep1.failures
    assert rep1.stored == first_lines
    assert sorted(await _check_ids(databases, project_id)) == [
        f"c{i:04d}" for i in range(first_lines)
    ]
    assert await _offset(service, project_id) == second_start
    assert [d.kind for _p, d, _t in incidents.signals] == ["parser_failure"]

    flag.unlink()
    rep2 = await service.collect(project_id, clock.now())

    assert rep2.failures == {}
    assert rep2.stored == lines - first_lines
    runs2 = _runs(runner)[2:]
    assert runs2[0][0] == second_start
    assert len(runs2) == 2
    _assert_line_pieces(data, [runs1[0], *runs2])
    assert sorted(await _check_ids(databases, project_id)) == [f"c{i:04d}" for i in range(lines)]
    assert await _offset(service, project_id) == len(data)


async def test_byte_lengths_stay_exact_past_a_byte_that_is_not_utf8(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    # 0xE9 alone (a cp1252 e-acute) decodes to U+FFFD, three bytes when re-encoded
    lines = [_line(i) for i in range(200)]
    lines[3] = lines[3][:-3] + b"\xe9x\n"
    data = b"".join(lines)
    (data_dir / "feed.raw").write_bytes(data)
    service, runner = await code_feed_service(
        databases, events, project_id, data_dir, "feed.raw", _script()
    )

    rep = await service.collect(project_id, clock.now())

    assert rep.failures == {}
    assert sorted(await _check_ids(databases, project_id)) == [f"c{i:04d}" for i in range(200)]
    _assert_line_pieces(data, _runs(runner))


async def test_line_longer_than_the_bound_is_one_piece_of_its_own(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    head = b"a1\na2\na3\n"
    long_line = b"L" * (2 * CODE_PARSER_CHUNK_BYTES + 100) + b"\n"
    # The last line has no newline; the file is quiet, so it ends the last piece
    tail = b"b1\nb2\nb3"
    data = head + long_line + tail
    target = data_dir / "feed.raw"
    target.write_bytes(data)
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    set_mtime(target, now - timedelta(seconds=HELD_TAIL_QUIET_SECONDS + 1))
    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)

    outcome = await FileSourceReader(connections).read_raw(
        project_id, source_id("file", "feed.raw", "conn_feed"), cursor={}, now=now
    )

    assert outcome.last_error is None
    pieces = [(c.start_offset, c.new_offset, c.content) for c in outcome.chunks]
    long_end = len(head) + len(long_line)
    assert pieces == [
        (0, len(head), head.decode()),
        (len(head), long_end, long_line.decode()),
        (long_end, len(data), tail.decode()),
    ]
    assert all(c.size == len(data) for c in outcome.chunks)
    assert outcome.cursor["files"]["feed.raw"]["offset"] == len(data)
