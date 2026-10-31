"""A collection cycle runs a bounded number of code parser chunks per file and prepares the
sandbox once (I22)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import select

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results import file_source
from mlops_agents.monitoring.results.code_parser import CodeParserRunner
from mlops_agents.monitoring.results.constants import (
    CODE_PARSER_CHUNK_BYTES,
    CODE_PARSER_MAX_CHUNKS_PER_CYCLE,
)
from mlops_agents.monitoring.results.contract import ParserMapping, ParserSpec, source_id
from mlops_agents.monitoring.results.file_source import FileSourceReader, read_code_parser_pieces
from mlops_agents.monitoring.results.parser_kind import CODE_PARSER_CONTRACT
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import results
from tests.monitoring.results.conftest import (
    STDIN_LINES_SCRIPT,
    FakeConnections,
    FakeSandbox,
    LocalExecDockerRunner,
    MountsConnections,
    code_feed_service,
    feed_mount,
    mapping_feed_service,
)
from tests.providers.fake_clock import FakeClock

_LINE_BYTES = 100
# Lines per piece: a piece is at most CODE_PARSER_CHUNK_BYTES and ends at a newline
_PIECE_LINES = CODE_PARSER_CHUNK_BYTES // _LINE_BYTES


def _lines(prefix: str, count: int) -> bytes:
    return b"".join(
        f"{prefix}{i:04d}".ljust(_LINE_BYTES - 1, "x").encode() + b"\n" for i in range(count)
    )


def _bound(monkeypatch: pytest.MonkeyPatch, chunks: int) -> None:
    """Sets the per-cycle chunk bound where the reader reads it."""
    monkeypatch.setattr(file_source, "CODE_PARSER_MAX_CHUNKS_PER_CYCLE", chunks, raising=False)


async def _check_ids(databases: Databases, project_id: str) -> list[str]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(results))).mappings().all()
    return [r["check_id"] for r in rows]


async def _offset(service: ResultsService, project_id: str, source: str, name: str) -> int:
    state = await service.store.get_source_state(project_id, source)
    assert state is not None
    return state.cursor.get("files", {}).get(name, {}).get("offset", 0)


async def test_file_needing_more_chunks_than_the_bound_is_read_over_several_cycles(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bound(monkeypatch, 2)
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    # Five pieces: two cycles of two, then one of one
    data = _lines("c", 4 * _PIECE_LINES + 10)
    (data_dir / "feed.raw").write_bytes(data)
    service, runner = await code_feed_service(
        databases, events, project_id, data_dir, "feed.raw", STDIN_LINES_SCRIPT
    )

    offsets: list[int] = []
    runs: list[int] = []
    stored = 0
    for _cycle in range(4):
        calls_before = len(runner.calls)
        rep = await service.collect(project_id, clock.now())
        assert rep.failures == {}
        stored += rep.stored
        runs.append(len(runner.calls) - calls_before)
        offsets.append(await _offset(service, project_id, "file:conn_feed:feed.raw", "feed.raw"))

    assert runs == [2, 2, 1, 0]
    assert offsets[0] < offsets[1] < offsets[2] == offsets[3] == len(data)
    assert offsets[0] == 2 * _PIECE_LINES * _LINE_BYTES
    check_ids = await _check_ids(databases, project_id)
    expected = [f"c{i:04d}".ljust(_LINE_BYTES - 1, "x") for i in range(4 * _PIECE_LINES + 10)]
    assert sorted(check_ids) == expected
    assert stored == len(expected)


async def test_read_raw_gives_a_code_parser_at_most_the_bound_of_chunks(
    tmp_path: Path, project_id: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound(monkeypatch, 3)
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    data = _lines("c", 20 * _PIECE_LINES)
    (data_dir / "feed.raw").write_bytes(data)
    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)

    outcome = await FileSourceReader(connections).read_raw(
        project_id, source_id("file", "feed.raw", "conn_feed"), cursor={}, now=clock.now()
    )

    assert outcome.last_error is None
    piece = _PIECE_LINES * _LINE_BYTES
    assert [(c.start_offset, c.new_offset) for c in outcome.chunks] == [
        (0, piece),
        (piece, 2 * piece),
        (2 * piece, 3 * piece),
    ]
    assert all(c.size == len(data) for c in outcome.chunks)
    assert outcome.cursor["files"]["feed.raw"]["offset"] == 3 * piece


async def test_a_cycle_prepares_the_sandbox_once_for_every_chunk_and_file(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    # Two pieces in each file
    (data_dir / "a.raw").write_bytes(_lines("a", _PIECE_LINES + 5))
    (data_dir / "b.raw").write_bytes(_lines("b", _PIECE_LINES + 5))
    sandbox = FakeSandbox(tag="sandbox:test-tag")
    connections = MountsConnections([feed_mount(data_dir)])
    service, runner = await code_feed_service(
        databases,
        events,
        project_id,
        data_dir,
        "*.raw",
        STDIN_LINES_SCRIPT,
        sandbox=sandbox,
        connections=connections,
    )

    rep1 = await service.collect(project_id, clock.now())

    assert rep1.failures == {}
    assert rep1.stored == 2 * (_PIECE_LINES + 5)
    assert len(runner.calls) == 4
    assert (sandbox.rebuilds, connections.mount_lookups) == (1, 1)

    # The next cycle prepares again: what a cycle prepared is dropped when it ends
    with (data_dir / "a.raw").open("ab") as f:
        f.write(_lines("z", 1))
    rep2 = await service.collect(project_id, clock.now())

    assert rep2.failures == {}
    assert rep2.stored == 1
    assert (sandbox.rebuilds, connections.mount_lookups) == (2, 2)


@pytest.mark.parametrize("failure", ["sandbox not ready", "mounts lookup raises"])
async def test_a_failed_preparation_fails_the_rest_of_the_cycle_without_preparing_again(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
    failure: str,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    (data_dir / "a.raw").write_bytes(_lines("a", 3))
    (data_dir / "b.raw").write_bytes(_lines("b", 3))
    sandbox = FakeSandbox(tag="sandbox:test-tag", ready=failure != "sandbox not ready")
    connections = MountsConnections(
        [feed_mount(data_dir)],
        mounts_error="mounts are down" if failure == "mounts lookup raises" else None,
    )
    service, runner = await code_feed_service(
        databases,
        events,
        project_id,
        data_dir,
        "*.raw",
        STDIN_LINES_SCRIPT,
        sandbox=sandbox,
        connections=connections,
    )

    rep = await service.collect(project_id, clock.now())

    source = "file:conn_feed:*.raw"
    expected_error = "Sandbox build failed" if failure == "sandbox not ready" else "mounts are down"
    assert expected_error in rep.failures[source]
    assert rep.stored == 0
    assert runner.calls == []
    assert sandbox.rebuilds == 1
    assert connections.mount_lookups == (0 if failure == "sandbox not ready" else 1)
    for name in ("a.raw", "b.raw"):
        assert await _offset(service, project_id, source, name) == 0


async def test_a_mapping_only_cycle_never_prepares_the_sandbox(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "feed"
    data_dir.mkdir()
    (data_dir / "feed.jsonl").write_bytes(b'{"id": "c1", "time": "2026-10-30T10:00:00Z"}\n')
    sandbox = FakeSandbox(tag="sandbox:test-tag")
    connections = MountsConnections([feed_mount(data_dir)])
    runner = CodeParserRunner(
        sandbox=sandbox,
        docker_runner=LocalExecDockerRunner("sandbox:test-tag", [feed_mount(data_dir)]),
        connections=connections,
    )
    parsers = SqliteParsers(databases, code_runner=runner)
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_feed:feed.jsonl",
            mapping=ParserMapping(
                format="jsonl",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="prop_mapping",
    )
    service = mapping_feed_service(databases, events, data_dir, "feed.jsonl", parsers)

    rep = await service.collect(project_id, clock.now())

    assert rep.failures == {}
    assert rep.stored == 1
    assert (sandbox.rebuilds, connections.mount_lookups) == (0, 0)


class _CountingFile(io.BytesIO):
    """A file that sums the bytes its reads return."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size: int | None = -1, /) -> bytes:
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def test_the_pieces_read_only_this_cycles_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _bound(monkeypatch, 4)
    data = _lines("c", 100 * _PIECE_LINES)
    offset = 3 * _PIECE_LINES * _LINE_BYTES
    f = _CountingFile(data)

    pieces = read_code_parser_pieces(f, offset, len(data), quiet=False)

    piece = _PIECE_LINES * _LINE_BYTES
    assert [(start, len(chunk)) for start, chunk in pieces] == [
        (offset + i * piece, piece) for i in range(4)
    ]
    assert all(chunk == data[start : start + len(chunk)] for start, chunk in pieces)
    last_end = pieces[-1][0] + len(pieces[-1][1])
    assert f.bytes_read <= last_end - offset + CODE_PARSER_CHUNK_BYTES
    assert f.bytes_read < len(data) // 10


def test_the_contract_says_whole_lines_and_the_per_cycle_bound() -> None:
    assert "whole lines" in CODE_PARSER_CONTRACT
    assert "quoted newlines" in CODE_PARSER_CONTRACT
    assert "mapping parser" in CODE_PARSER_CONTRACT
    assert f"at most {CODE_PARSER_MAX_CHUNKS_PER_CYCLE} chunks of a file" in CODE_PARSER_CONTRACT
