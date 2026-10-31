"""A last record without its newline: read only once its file has been quiet for the quiet
time and the record is complete (I22)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.constants import HELD_TAIL_QUIET_SECONDS
from mlops_agents.monitoring.results.contract import ParserMapping, ParserSpec
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import results
from tests.monitoring.results.conftest import (
    STDIN_LINES_SCRIPT,
    FakeIncidents,
    FakeObserver,
    code_feed_service,
    mapping_feed_service,
    set_mtime,
)
from tests.providers.fake_clock import FakeClock

_QUIET = timedelta(seconds=HELD_TAIL_QUIET_SECONDS + 1)
# One collection interval at its minimum: unchanged that long is not quiet
_INTERVAL_SECONDS = 10

_DEFAULTS: dict[str, Any] = {"category": "data_quality", "subject": "tbl"}
_NEVER_FIRES = Compare(op="eq", field="fired", value=True)
_FIELDS = {"check_id": "id", "ran_at": "time", "value": "value"}


def _agent_line(check_id: str, ran_at: str, value: float) -> bytes:
    """A standard result line, its value last so a cut before the end truncates the number."""
    record: dict[str, Any] = {
        "check_id": check_id,
        "origin": "agent",
        "ran_at": ran_at,
        "fired": False,
        "category": "data_quality",
        "subject": "tbl",
        "detail": {},
        "source_ref": "r",
        "value": value,
    }
    return json.dumps(record).encode()


@dataclass(frozen=True)
class _Case:
    path: str  # the file, relative to the connection folder
    source: str  # its source ID
    mapping: ParserMapping | None  # None: the standard reader
    first: bytes  # complete records (csv: the header and one row), each with its newline
    whole: bytes  # the next record, c2 with value 0.95, complete but without its newline
    cut: int  # where a buffered writer's flush splits `whole`: before it, an incomplete record
    c1_offset: int = 0  # where c1, the first record of `first`, starts

    @property
    def c1_key(self) -> str:
        return f"{self.path}:{self.c1_offset}"

    @property
    def half(self) -> bytes:
        return self.whole[: self.cut]

    @property
    def rest(self) -> bytes:
        return self.whole[self.cut :] + b"\n"


_CSV_WHOLE = b"c2,2026-10-30T10:05:00Z,0.95"
_JSONL_WHOLE = b'{"id": "c2", "time": "2026-10-30T10:05:00Z", "value": 0.95}'
_LOG_WHOLE = b"2026-10-30T10:05:00Z [CHECK:c2] value=0.95"
_STANDARD_WHOLE = _agent_line("c2", "2026-10-30T10:05:00Z", 0.95)

_CASES: dict[str, _Case] = {
    # The half record has two fields against the header's three
    "csv": _Case(
        path="feed.csv",
        source="file:conn_feed:feed.csv",
        mapping=ParserMapping(
            format="csv", fields=_FIELDS, fired_when=_NEVER_FIRES, defaults=_DEFAULTS
        ),
        first=b"id,time,value\nc1,2026-10-30T10:00:00Z,0.5\n",
        whole=_CSV_WHOLE,
        cut=_CSV_WHOLE.rindex(b","),
        c1_offset=len(b"id,time,value\n"),
    ),
    # The half record is cut inside its last number: not valid JSON
    "jsonl": _Case(
        path="feed.jsonl",
        source="file:conn_feed:feed.jsonl",
        mapping=ParserMapping(
            format="jsonl", fields=_FIELDS, fired_when=_NEVER_FIRES, defaults=_DEFAULTS
        ),
        first=b'{"id": "c1", "time": "2026-10-30T10:00:00Z", "value": 0.5}\n',
        whole=_JSONL_WHOLE,
        cut=len(_JSONL_WHOLE) - 2,
    ),
    # The half line ends in "value=0.", which the pattern does not match yet
    "log": _Case(
        path="feed.log",
        source="file:conn_feed:feed.log",
        mapping=ParserMapping(
            format="log",
            pattern=r"^(?P<time>\S+) \[CHECK:(?P<id>\w+)\] value=(?P<value>\d+\.\d+)$",
            fields=_FIELDS,
            fired_when=_NEVER_FIRES,
            defaults=_DEFAULTS,
        ),
        first=b"2026-10-30T10:00:00Z [CHECK:c1] value=0.5\n",
        whole=_LOG_WHOLE,
        cut=len(_LOG_WHOLE) - 2,
    ),
    # The standard results location, read without a parser
    "standard": _Case(
        path="standard/results.jsonl",
        source="file:conn_feed:standard/*.jsonl",
        mapping=None,
        first=_agent_line("c1", "2026-10-30T10:00:00Z", 0.5) + b"\n",
        whole=_STANDARD_WHOLE,
        cut=len(_STANDARD_WHOLE) - 2,
    ),
}
_MAPPING_FORMATS = sorted(name for name, case in _CASES.items() if case.mapping is not None)


async def _service(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
    case: _Case,
    incidents: FakeIncidents | None = None,
) -> tuple[ResultsService, Path]:
    """A service reading `case`'s file, which holds `case.first`, just written."""
    data_dir = tmp_path / "data"
    target = data_dir / case.path
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, case.first, clock)

    parsers = SqliteParsers(databases)
    if case.mapping is not None:
        await parsers.store(
            project_id,
            ParserSpec(source_id=case.source, mapping=case.mapping),
            proposal_id="prop_tail",
        )
    # The standard case reads its file as the results location; the feed's file is absent
    feed_pattern = case.path if case.mapping is not None else "absent.csv"
    service = mapping_feed_service(databases, events, data_dir, feed_pattern, parsers, incidents)
    return service, target


def _write(target: Path, data: bytes, clock: FakeClock, *, quiet: bool = False) -> None:
    """Appends `data`; the file then counts as changed now, or long enough ago to be quiet."""
    with target.open("ab") as f:
        f.write(data)
    set_mtime(target, clock.now() - _QUIET if quiet else clock.now())


async def _stored(databases: Databases, project_id: str) -> list[tuple[str, str, float | None]]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(results))).mappings().all()
    return sorted((r["check_id"], r["source_key"], r["value"]) for r in rows)


@pytest.mark.parametrize("fmt", sorted(_CASES))
async def test_half_record_waits_for_its_writer_and_arrives_whole_once(
    fmt: str,
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    case = _CASES[fmt]
    service, target = await _service(databases, events, project_id, tmp_path, clock, case)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1

    # A buffered writer flushes half a record and idles: unchanged, but not for the quiet time
    _write(target, case.half, clock)
    for _ in range(2):
        clock.advance(_INTERVAL_SECONDS)
        rep = await service.collect(project_id, clock.now())
        assert rep.failures == {}
        assert rep.stored == 0

    _write(target, case.rest, clock)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0

    assert await _stored(databases, project_id) == [
        ("c1", case.c1_key, 0.5),
        ("c2", f"{case.path}:{len(case.first)}", 0.95),
    ]


@pytest.mark.parametrize("fmt", sorted(_CASES))
async def test_last_record_without_newline_arrives_once_the_writer_stops(
    fmt: str,
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    case = _CASES[fmt]
    service, target = await _service(databases, events, project_id, tmp_path, clock, case)
    observer = FakeObserver()
    service.add_observer(observer)
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1

    # Complete, but the file changed less than the quiet time ago: the writer may not be done
    _write(target, case.whole, clock)
    for _ in range(2):
        clock.advance(_INTERVAL_SECONDS)
        rep = await service.collect(project_id, clock.now())
        assert rep.failures == {}
        assert rep.stored == 0

    # Quiet for the quiet time: the complete last record arrives, once
    set_mtime(target, clock.now() - _QUIET)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0

    delivered = [(r.check_id, r.source_key) for batch in observer.batches for r in batch]
    assert delivered == [("c1", case.c1_key), ("c2", f"{case.path}:{len(case.first)}")]


@pytest.mark.parametrize("fmt", _MAPPING_FORMATS)
async def test_incomplete_last_record_after_the_quiet_time_fails_at_its_offset(
    fmt: str,
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    case = _CASES[fmt]
    incidents = FakeIncidents()
    service, target = await _service(
        databases, events, project_id, tmp_path, clock, case, incidents
    )
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1

    # The writer flushed half a record and stopped for good
    _write(target, case.half, clock, quiet=True)
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0
    assert f"Offset {len(case.first)}" in rep.failures[case.source]
    failures = [d for _p, d, _t in incidents.signals if d.kind == "parser_failure"]
    assert len(failures) == 1
    assert f"Offset {len(case.first)}" in str(failures[0].detail["error"])

    # The cursor moved past it: it fails once, and a later good record is stored
    state = await service.store.get_source_state(project_id, case.source)
    assert state is not None
    broken_end = len(case.first) + case.cut
    assert state.cursor["files"][case.path]["offset"] == broken_end
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}

    _write(target, b"\n" + case.whole + b"\n", clock)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    assert await _stored(databases, project_id) == [
        ("c1", case.c1_key, 0.5),
        ("c2", f"{case.path}:{broken_end + 1}", 0.95),
    ]


async def test_standard_incomplete_last_line_after_the_quiet_time_is_an_invalid_line(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    case = _CASES["standard"]
    service, target = await _service(databases, events, project_id, tmp_path, clock, case)
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1

    _write(target, case.half, clock, quiet=True)
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0
    assert f"Offset {len(case.first)}" in rep.failures[case.source]

    state = await service.store.get_source_state(project_id, case.source)
    assert state is not None
    broken_end = len(case.first) + case.cut
    assert state.cursor["files"][case.path]["offset"] == broken_end

    _write(target, b"\n" + case.whole + b"\n", clock)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    assert await _stored(databases, project_id) == [
        ("c1", case.c1_key, 0.5),
        ("c2", f"{case.path}:{broken_end + 1}", 0.95),
    ]


@pytest.mark.parametrize("ending", [b"", b"\n"], ids=["no_newline", "newline"])
async def test_unclosed_quote_at_the_end_raises_parser_failure_with_its_offset(
    ending: bytes,
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    case = _CASES["csv"]
    incidents = FakeIncidents()
    service, target = await _service(
        databases, events, project_id, tmp_path, clock, case, incidents
    )
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1

    # Not quiet yet: the quote may still be closed
    _write(target, b'c2,2026-10-30T10:05:00Z,"0.9' + ending, clock)
    for _ in range(2):
        clock.advance(_INTERVAL_SECONDS)
        rep = await service.collect(project_id, clock.now())
        assert rep.failures == {}
        assert rep.stored == 0
    assert incidents.signals == []

    # Quiet for the quiet time with the quote still open: the record is broken
    set_mtime(target, clock.now() - _QUIET)
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0
    assert f"Offset {len(case.first)}" in rep.failures[case.source]
    failures = [d for _p, d, _t in incidents.signals if d.kind == "parser_failure"]
    assert len(failures) == 1
    assert f"Offset {len(case.first)}" in str(failures[0].detail["error"])

    # The cursor moved past the broken record: a later good one is stored
    _write(target, b"\nc3,2026-10-30T10:10:00Z,0.5\n", clock)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    assert [check_id for check_id, _k, _v in await _stored(databases, project_id)] == ["c1", "c3"]


async def test_code_parser_gets_a_last_line_without_newline_only_after_the_quiet_time(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.raw"
    _write(target, b"c1\n", clock)
    service, docker_runner = await code_feed_service(
        databases, events, project_id, data_dir, "feed.raw", STDIN_LINES_SCRIPT
    )
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1
    runs = len(docker_runner.calls)

    # Not quiet: the script is not given the line at all
    _write(target, b"c2", clock)
    for _ in range(2):
        clock.advance(_INTERVAL_SECONDS)
        rep = await service.collect(project_id, clock.now())
        assert rep.failures == {}
        assert rep.stored == 0
    assert len(docker_runner.calls) == runs

    # Quiet for the quiet time: the script gets it, once, and judges it
    set_mtime(target, clock.now() - _QUIET)
    rep = await service.collect(project_id, clock.now())
    assert rep.failures == {}
    assert rep.stored == 1
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0
    assert len(docker_runner.calls) == runs + 1

    assert await _stored(databases, project_id) == [
        ("c1", "feed.raw:0:0", None),
        ("c2", "feed.raw:3:0", None),
    ]
