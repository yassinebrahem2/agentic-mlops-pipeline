"""Table profiles: statistics, caps, the timeout and unreadable tables."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest

from mlops_agents.connections.contract import DuckDbSource
from mlops_agents.data import profile as profile_module
from mlops_agents.data.constants import MAX_COLUMNS, SAMPLE_ROWS, TEXT_CUT, TOP_VALUES
from mlops_agents.data.duck import DEADLINE_THREAD_NAME, locked_connection
from mlops_agents.data.profile import ColumnProfile, TableProfile, profile_table
from mlops_agents.errors import DomainError
from tests.data.conftest import TeamFolder, write_parquet

ENDLESS = "range(10000000000000)"


def _column(profile: TableProfile, name: str) -> ColumnProfile:
    return next(c for c in profile.columns if c.name == name)


@pytest.mark.parametrize(
    ("table", "rows"),
    [("people", 3), ("scores", 4), ("events", 2), ("logs", 5), ("sales", 6)],
)
async def test_every_fixture_format_profiles(
    fixture_tables: TeamFolder, table: str, rows: int
) -> None:
    profile = await profile_table(fixture_tables.source(table))
    assert profile.row_count == rows
    assert not profile.timed_out
    assert len(profile.sample) == rows


async def test_row_and_null_counts(team: TeamFolder) -> None:
    (team.folder / "t.csv").write_text("a,b\n1,\n,x\n3,y\n4,\n", encoding="utf-8")
    profile = await profile_table(team.source("t"))
    assert profile.row_count == 4
    assert _column(profile, "a").nulls == 1
    assert _column(profile, "b").nulls == 2


async def test_distinct_is_approximate_within_ten_percent(team: TeamFolder) -> None:
    write_parquet(team.folder / "t.parquet", "SELECT range AS v FROM range(10000)")
    distinct = _column(await profile_table(team.source("t")), "v").distinct
    assert abs(distinct - 10_000) <= 1_000


async def test_min_and_max_of_numbers_dates_and_timestamps(team: TeamFolder) -> None:
    write_parquet(
        team.folder / "t.parquet",
        "SELECT * FROM (VALUES"
        " (3, 2.5::DOUBLE, DATE '2024-03-01', TIMESTAMP '2024-03-01 10:00:00',"
        "  TIMESTAMPTZ '2024-03-01 10:00:00+02'),"
        " (-7, -1.25::DOUBLE, DATE '2023-12-31', TIMESTAMP '2023-12-31 23:59:59',"
        "  TIMESTAMPTZ '2023-12-31 23:30:00-05'),"
        " (12, 9.0::DOUBLE, DATE '2024-01-15', TIMESTAMP '2024-01-15 00:00:00',"
        "  TIMESTAMPTZ '2024-01-15 00:00:00+00')"
        ") t(i, d, day, ts, tz)",
    )
    profile = await profile_table(team.source("t"))
    assert (_column(profile, "i").min, _column(profile, "i").max) == ("-7", "12")
    assert (_column(profile, "d").min, _column(profile, "d").max) == ("-1.25", "9.0")
    assert (_column(profile, "day").min, _column(profile, "day").max) == (
        "2023-12-31",
        "2024-03-01",
    )
    assert (_column(profile, "ts").min, _column(profile, "ts").max) == (
        "2023-12-31 23:59:59",
        "2024-03-01 10:00:00",
    )
    # In UTC whatever the host's zone, so a summary does not depend on where it was computed.
    assert (_column(profile, "tz").min, _column(profile, "tz").max) == (
        "2024-01-01 04:30:00+00",
        "2024-03-01 08:00:00+00",
    )


async def test_sample_cells_are_duckdbs_own_text(team: TeamFolder) -> None:
    """A Python value is never fetched: no pytz, no clamped infinity, no lost nanoseconds."""
    write_parquet(
        team.folder / "t.parquet",
        "SELECT * FROM (VALUES"
        " (TIMESTAMPTZ '2024-03-01 10:00:00+02', DATE 'infinity', TIMESTAMP 'infinity',"
        "  TIMESTAMP '-infinity', '2024-03-01 10:00:00.123456789'::TIMESTAMP_NS, 1.1::FLOAT,"
        "  true, [1, 2, NULL], {'a': 1, 'b': 'x'}, '\\x00\\xFFab'::BLOB),"
        " (NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
        ") t(tz, day, ts, ts_low, ns, f, flag, list, struct, blob)",
    )
    profile = await profile_table(team.source("t"))
    assert not profile.timed_out
    assert profile.sample == [
        [
            "2024-03-01 08:00:00+00",
            "infinity",
            "infinity",
            "-infinity",
            "2024-03-01 10:00:00.123456789",
            "1.1",
            "true",
            "[1, 2, NULL]",
            "{'a': 1, 'b': x}",
            r"\x00\xFFab",
        ],
        ["NULL"] * 10,
    ]


async def test_top_values_of_text(team: TeamFolder) -> None:
    long = "x" * 200
    values = ["b"] * 3 + ["a"] * 3 + ["c"] * 2 + [long] * 2 + ["e", "d"] + [None] * 5
    rows = ", ".join("(NULL)" if v is None else f"('{v}')" for v in values)
    write_parquet(team.folder / "t.parquet", f"SELECT * FROM (VALUES {rows}) t(v)")
    column = _column(await profile_table(team.source("t")), "v")
    assert column.top_values == ["a", "b", "c", "x" * TEXT_CUT, "d"]
    assert len(column.top_values) == TOP_VALUES
    assert column.min is None
    assert column.max is None


async def test_a_boolean_has_no_range_or_top_values(team: TeamFolder) -> None:
    write_parquet(
        team.folder / "t.parquet", "SELECT * FROM (VALUES (true), (false), (NULL)) t(flag)"
    )
    column = _column(await profile_table(team.source("t")), "flag")
    assert column.type == "BOOLEAN"
    assert (column.min, column.max, column.top_values) == (None, None, [])
    assert column.nulls == 1
    assert column.distinct == 2


async def test_only_the_first_columns_are_profiled(team: TeamFolder) -> None:
    columns = ", ".join(f"range AS c{i}" for i in range(60))
    write_parquet(team.folder / "t.parquet", f"SELECT {columns} FROM range(3)")
    profile = await profile_table(team.source("t"))
    assert len(profile.columns) == MAX_COLUMNS
    assert profile.more_columns == 10
    assert profile.sample_columns == [f"c{i}" for i in range(MAX_COLUMNS)]
    assert all(len(row) == MAX_COLUMNS for row in profile.sample)


async def test_the_sample_is_capped_and_cut(team: TeamFolder) -> None:
    write_parquet(
        team.folder / "t.parquet",
        "SELECT range AS n, repeat('y', 500) AS text,"
        " CASE WHEN range = 0 THEN NULL ELSE range END AS maybe FROM range(100000)",
    )
    profile = await profile_table(team.source("t"))
    assert profile.row_count == 100_000
    assert len(profile.sample) == SAMPLE_ROWS
    by_name = dict(zip(profile.sample_columns, zip(*profile.sample, strict=True), strict=True))
    assert all(cell == "y" * TEXT_CUT for cell in by_name["text"])
    assert "NULL" in by_name["maybe"]


async def test_a_slow_table_times_out_with_its_columns(
    team: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[duckdb.DuckDBPyConnection] = []

    def spy(allowed: list[Path]) -> duckdb.DuckDBPyConnection:
        connection = locked_connection(allowed)
        opened.append(connection)
        return connection

    monkeypatch.setattr(profile_module, "locked_connection", spy)
    started = time.monotonic()
    profile = await profile_table(
        DuckDbSource(sql_from=ENDLESS, allowed_directories=[team.folder]), timeout_s=0.2
    )
    assert time.monotonic() - started < 1
    assert profile.timed_out
    assert profile.timeout_s == 0.2
    assert profile.row_count is None  # not computed, never reported as 0 rows
    assert [(c.name, c.type) for c in profile.columns] == [("range", "BIGINT")]
    assert len(opened) == 1
    with pytest.raises(duckdb.ConnectionException):
        opened[0].execute("SELECT 1")


class _CloseSpy:
    """A connection that records which deadline watchdogs are alive when it is closed."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection
        self.watchdogs_at_close: list[threading.Thread] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def close(self) -> None:
        self.watchdogs_at_close = [
            t for t in threading.enumerate() if t.name == DEADLINE_THREAD_NAME
        ]
        self._connection.close()


@pytest.mark.parametrize(("sql_from", "timed_out"), [("range(3)", False), (ENDLESS, True)])
async def test_the_watchdog_stops_before_the_connection_closes(
    team: TeamFolder, monkeypatch: pytest.MonkeyPatch, sql_from: str, timed_out: bool
) -> None:
    """DuckDB raises on an interrupt after close, so the watchdog must be gone by then."""
    spies: list[_CloseSpy] = []

    def spy(allowed: list[Path]) -> duckdb.DuckDBPyConnection:
        spies.append(_CloseSpy(locked_connection(allowed)))
        return cast(duckdb.DuckDBPyConnection, spies[-1])

    monkeypatch.setattr(profile_module, "locked_connection", spy)
    profile = await profile_table(
        DuckDbSource(sql_from=sql_from, allowed_directories=[team.folder]), timeout_s=0.2
    )
    assert profile.timed_out is timed_out
    assert [s.watchdogs_at_close for s in spies] == [[]]


async def test_the_default_timeout_is_the_constant(team: TeamFolder) -> None:
    (team.folder / "t.csv").write_text("a\n1\n", encoding="utf-8")
    profile = await profile_table(team.source("t"))
    assert profile.timeout_s == profile_module.PROFILE_TIMEOUT_S


async def test_the_event_loop_keeps_running_during_a_profile(team: TeamFolder) -> None:
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    profile = await profile_table(
        DuckDbSource(sql_from=ENDLESS, allowed_directories=[team.folder]), timeout_s=0.3
    )
    done.set()
    await task
    assert profile.timed_out
    assert ticks >= 10


async def test_a_missing_file_is_unreadable(team: TeamFolder) -> None:
    (team.folder / "t.csv").write_text("a\n1\n", encoding="utf-8")
    source = team.source("t")
    (team.folder / "t.csv").unlink()
    with pytest.raises(DomainError) as err:
        await profile_table(source)
    _assert_unreadable(err.value, team.home)


async def test_a_malformed_csv_is_unreadable(team: TeamFolder) -> None:
    (team.folder / "t.csv").write_bytes(b"a,b\n1,\xff\xfe\n")
    with pytest.raises(DomainError) as err:
        await profile_table(team.source("t"))
    _assert_unreadable(err.value, team.home)


async def test_an_unreadable_error_is_scrubbed(team: TeamFolder) -> None:
    """DuckDB quotes the path it refused, so a credential in it must be masked."""
    source = DuckDbSource(
        sql_from="read_csv(['https://user:PASSWORD-1@example.com/t.csv?token=TOKEN-2'])",
        allowed_directories=[team.folder],
    )
    with pytest.raises(DomainError) as err:
        await profile_table(source)
    _assert_unreadable(err.value, team.home)
    assert "example.com" in err.value.message
    assert "PASSWORD-1" not in err.value.message
    assert "TOKEN-2" not in err.value.message


def _assert_unreadable(error: DomainError, home: Path) -> None:
    assert error.code == "table_unreadable"
    assert error.status == 422
    assert error.message
    for text in (str(home), home.as_posix(), str(home.resolve()), home.resolve().as_posix()):
        assert text not in error.message
