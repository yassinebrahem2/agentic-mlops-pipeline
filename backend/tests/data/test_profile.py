"""Table profiles: statistics, caps, the timeout and unreadable tables."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import duckdb
import pytest

from mlops_agents.connections.contract import DuckDbSource
from mlops_agents.data import profile as profile_module
from mlops_agents.data.constants import MAX_COLUMNS, SAMPLE_ROWS, TEXT_CUT, TOP_VALUES
from mlops_agents.data.duck import locked_connection
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
        " (3, 2.5::DOUBLE, DATE '2024-03-01', TIMESTAMP '2024-03-01 10:00:00'),"
        " (-7, -1.25::DOUBLE, DATE '2023-12-31', TIMESTAMP '2023-12-31 23:59:59'),"
        " (12, 9.0::DOUBLE, DATE '2024-01-15', TIMESTAMP '2024-01-15 00:00:00')"
        ") t(i, d, day, ts)",
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
    assert [(c.name, c.type) for c in profile.columns] == [("range", "BIGINT")]
    assert len(opened) == 1
    with pytest.raises(duckdb.ConnectionException):
        opened[0].execute("SELECT 1")


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


def _assert_unreadable(error: DomainError, home: Path) -> None:
    assert error.code == "table_unreadable"
    assert error.status == 422
    assert error.message
    for text in (str(home), home.as_posix(), str(home.resolve()), home.resolve().as_posix()):
        assert text not in error.message
