"""Profiles under DuckDB's memory limit: near-unique text columns and out-of-memory errors
(Decision #432)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator

import duckdb
import pytest

from mlops_agents.data import profile as profile_module
from mlops_agents.data.constants import SAMPLE_ROWS, TOP_VALUES
from mlops_agents.data.profile import ColumnProfile, TableProfile, TableUnreadable, profile_table
from tests.data.conftest import TeamFolder, sql_text, write_parquet
from tests.data.summary_env import OUT_OF_MEMORY, out_of_memory_at

BIG_ROWS = 10_000_000
# The 10M-row file is about 10 MB; the margin covers DuckDB's own temporary files.
FREE_BYTES_NEEDED = 3 * 1024**3


def _column(profile: TableProfile, name: str) -> ColumnProfile:
    return next(c for c in profile.columns if c.name == name)


@pytest.fixture
def mixed(team: TeamFolder) -> TeamFolder:
    """100 rows: a unique text id, a text column of 80 values, one of 4 values, a number."""
    write_parquet(
        team.folder / "t.parquet",
        "SELECT 'id-' || range AS id, CAST(range % 80 AS VARCHAR) AS mostly,"
        " CAST(range % 4 AS VARCHAR) AS kind, range AS n FROM range(100)",
    )
    return team


async def test_a_nearly_unique_text_column_gets_no_top_values(mixed: TeamFolder) -> None:
    profile = await profile_table(mixed.source("t"))
    assert _column(profile, "id").distinct >= 0.95 * 100
    assert _column(profile, "id").top_values == []
    assert len(_column(profile, "mostly").top_values) == TOP_VALUES
    assert _column(profile, "kind").top_values == ["0", "1", "2", "3"]
    assert not profile.incomplete


async def test_out_of_memory_in_the_statistics_keeps_the_columns(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_of_memory_at(monkeypatch, "_aggregate")
    profile = await profile_table(mixed.source("t"))
    assert profile.incomplete
    assert not profile.timed_out
    assert profile.row_count is None  # not computed, never reported as 0 rows
    assert [c.name for c in profile.columns] == ["id", "mostly", "kind", "n"]
    assert profile.sample == []


async def test_out_of_memory_in_the_top_values_keeps_the_count_and_statistics(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_of_memory_at(monkeypatch, "_top_values")
    profile = await profile_table(mixed.source("t"))
    assert profile.incomplete
    assert not profile.timed_out
    assert profile.row_count == 100
    assert (_column(profile, "n").min, _column(profile, "n").max) == ("0", "99")
    assert _column(profile, "kind").distinct == 4
    assert (profile.sample_columns, profile.sample) == ([], [])


async def test_out_of_memory_in_the_sample_keeps_the_top_values(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_of_memory_at(monkeypatch, "_take_sample")
    profile = await profile_table(mixed.source("t"))
    assert profile.incomplete
    assert _column(profile, "kind").top_values == ["0", "1", "2", "3"]
    assert profile.sample == []


async def test_out_of_memory_is_known_by_its_text_too(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader can wrap DuckDB's memory error in another error class."""
    out_of_memory_at(monkeypatch, "_top_values", duckdb.InvalidInputException(OUT_OF_MEMORY))
    profile = await profile_table(mixed.source("t"))
    assert profile.incomplete
    assert profile.row_count == 100


async def test_out_of_memory_before_the_columns_are_known_is_unreadable(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing finished, so there is nothing to keep."""
    out_of_memory_at(monkeypatch, "_describe")
    with pytest.raises(TableUnreadable) as err:
        await profile_table(mixed.source("t"))
    assert err.value.code == "table_unreadable"
    assert err.value.message.startswith("Out of Memory Error")


async def test_another_error_in_a_later_step_stays_unreadable(
    mixed: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_of_memory_at(
        monkeypatch, "_top_values", duckdb.InvalidInputException("Invalid Input Error: bad cell")
    )
    with pytest.raises(TableUnreadable) as err:
        await profile_table(mixed.source("t"))
    assert err.value.message == "Invalid Input Error: bad cell"


@pytest.fixture(scope="module")
def big_ids(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TeamFolder]:
    """10M rows of one unique 64-character text column, an id column, in one Parquet file
    that DuckDB writes itself (about 10 MB) and that is deleted after the module."""
    root = tmp_path_factory.mktemp("big ids")
    if shutil.disk_usage(root).free < FREE_BYTES_NEEDED:
        pytest.skip("less than 3 GB free on the temporary folder's drive")
    team = TeamFolder(home=root / "home", folder=root / "team data")
    team.home.mkdir()
    team.folder.mkdir()
    target = team.folder / "ids.parquet"
    spill = root / "spill"  # never the working folder's .tmp
    try:
        with duckdb.connect(":memory:", config={"temp_directory": str(spill)}) as setup:
            setup.execute(
                f"COPY (SELECT lpad(CAST(range AS VARCHAR), 64, '0') AS id FROM range({BIG_ROWS}))"
                f" TO {sql_text(target)} (FORMAT parquet, COMPRESSION zstd)"
            )
        yield team
    finally:
        target.unlink(missing_ok=True)
        shutil.rmtree(spill, ignore_errors=True)


async def test_a_large_unique_id_column_profiles_without_top_values(big_ids: TeamFolder) -> None:
    profile = await profile_table(big_ids.source("ids"))
    assert not profile.timed_out
    assert not profile.incomplete
    assert profile.row_count == BIG_ROWS
    (column,) = profile.columns
    assert (column.name, column.type, column.top_values) == ("id", "VARCHAR", [])
    assert len(profile.sample) == SAMPLE_ROWS


async def test_a_real_out_of_memory_error_keeps_what_finished(
    big_ids: TeamFolder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the near-unique rule off, the id column's grouping passes the memory limit."""
    monkeypatch.setattr(profile_module, "NEARLY_UNIQUE_SHARE", 2.0)
    profile = await profile_table(big_ids.source("ids"))
    assert profile.incomplete
    assert not profile.timed_out
    assert profile.row_count == BIG_ROWS
    (column,) = profile.columns
    assert (column.name, column.type, column.top_values) == ("id", "VARCHAR", [])
    assert column.distinct >= 0.95 * BIG_ROWS
