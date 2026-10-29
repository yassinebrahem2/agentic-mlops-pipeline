"""The locked DuckDB session: what it refuses, what it still reads, and hostile names."""

from __future__ import annotations

import asyncio
import csv
import os
import time
from pathlib import Path

import duckdb
import pytest

from mlops_agents.config import DUCKDB_VERSION
from mlops_agents.data.duck import (
    Deadline,
    DeadlineExpired,
    locked_connection,
    posix_directory,
    quote_identifier,
)
from mlops_agents.data.profile import TableProfile, profile_table
from tests.data.conftest import TeamFolder, folder_state

FIXTURE_ROWS = {"people": 3, "scores": 4, "events": 2, "logs": 5, "sales": 6}


def test_duckdb_is_the_pinned_version() -> None:
    assert duckdb.__version__ == DUCKDB_VERSION


def test_the_configuration_is_locked_and_autoloading_off(team: TeamFolder) -> None:
    with locked_connection([team.folder]) as connection:
        row = connection.execute(
            "SELECT current_setting('lock_configuration'),"
            " current_setting('enable_external_access'),"
            " current_setting('autoload_known_extensions'),"
            " current_setting('autoinstall_known_extensions'),"
            " current_setting('TimeZone')"
        ).fetchone()
    assert row == (True, False, False, False, "UTC")


@pytest.mark.parametrize("table", sorted(FIXTURE_ROWS))
def test_a_table_inside_the_folder_reads(fixture_tables: TeamFolder, table: str) -> None:
    source = fixture_tables.source(table)
    with locked_connection(source.allowed_directories) as connection:
        row = connection.execute(f"SELECT count(*) FROM {source.sql_from}").fetchone()
    assert row == (FIXTURE_ROWS[table],)


def test_a_folder_name_with_a_quote_is_bound_not_spliced(tmp_path: Path) -> None:
    folder = tmp_path / "it's data"
    folder.mkdir()
    (folder / "t.csv").write_text("a\n1\n", encoding="utf-8")
    path = (folder / "t.csv").resolve().as_posix().replace("'", "''")
    with locked_connection([folder]) as connection:
        assert connection.execute(f"SELECT a FROM read_csv(['{path}'])").fetchall() == [(1,)]


@pytest.mark.skipif(os.name != "nt", reason="the \\\\?\\ prefix exists only on Windows")
def test_the_extended_length_prefix_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.path.realpath(tmp_path)

    def extended(self: Path, strict: bool = False) -> Path:
        return Path("\\\\?\\" + real)

    monkeypatch.setattr(Path, "resolve", extended)
    assert posix_directory(tmp_path) == Path(real).as_posix()


# {inside} is the allowed folder, {outside} its parent; both as quoted-ready POSIX text.
# Each case names the error the lock gives, so a typo or a missing network cannot pass it.
_FILE_LOCK = (duckdb.PermissionException, "disabled (by|through) configuration")
_CONFIG_LOCK = (duckdb.InvalidInputException, "configuration has been locked")
REFUSED: dict[str, tuple[str, tuple[type[duckdb.Error], str]]] = {
    "read_relative": ("SELECT * FROM read_csv(['outside.csv'])", _FILE_LOCK),
    "read_dot_dot": ("SELECT * FROM read_csv(['{inside}/../outside.csv'])", _FILE_LOCK),
    "read_absolute": ("SELECT * FROM read_csv(['{outside}/outside.csv'])", _FILE_LOCK),
    "set_external_access": ("SET enable_external_access = true", _CONFIG_LOCK),
    "set_allowed_directories": ("SET allowed_directories = ['{outside}']", _CONFIG_LOCK),
    "set_lock_configuration": ("SET lock_configuration = false", _CONFIG_LOCK),
    "set_time_zone": ("SET TimeZone = 'America/New_York'", _CONFIG_LOCK),
    "install": ("INSTALL httpfs", _FILE_LOCK),
    "load": ("LOAD httpfs", _FILE_LOCK),
    "attach_outside": ("ATTACH '{outside}/attached.db' AS outside_db", _FILE_LOCK),
    "copy_absolute": ("COPY (SELECT 1) TO '{outside}/copied.csv'", _FILE_LOCK),
    "copy_dot_dot": ("COPY (SELECT 1) TO '{inside}/../copied_up.csv'", _FILE_LOCK),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_the_lockdown_refuses(team: TeamFolder, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    outside = team.folder.parent
    (outside / "outside.csv").write_text("a\n1\n", encoding="utf-8")
    monkeypatch.chdir(outside)
    before = folder_state(outside)
    template, (error, message) = REFUSED[case]
    sql = template.format(
        inside=team.folder.resolve().as_posix().replace("'", "''"),
        outside=outside.resolve().as_posix().replace("'", "''"),
    )
    with locked_connection([team.folder]) as connection, pytest.raises(error, match=message):
        connection.execute(sql)
    assert folder_state(outside) == before


async def test_hostile_file_and_column_names_change_nothing(team: TeamFolder) -> None:
    # A file name cannot hold a path separator, so its payload's target stays relative.
    file_name = "a'); COPY (SELECT 1) TO 'pwned.csv'; --.csv"
    # The headers aim inside the allowed folder, the one place the lock-down still lets
    # DuckDB write. The first one closes the aggregate query into a valid statement, so
    # with broken quoting it really writes, and the folder check below is what fails.
    inside = team.folder.resolve().as_posix()
    headers = [
        f'a") FROM range(1) t("a"); COPY (SELECT 1) TO \'{inside}/pwned_by_header.csv\'; --',
        f"x\"); ATTACH '{inside}/evil.db' AS e; CREATE TABLE e.t AS SELECT 1; --",
        'say "hi"',
        "it's",
        f"\"); COPY (SELECT 1) TO '{inside}/pwned2.csv'; --",
    ]
    with (team.folder / file_name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows([[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]])
    before = folder_state(team.folder.parent)
    source = team.source(file_name.removesuffix(".csv"))

    (outcome,) = await asyncio.gather(profile_table(source), return_exceptions=True)

    assert folder_state(team.folder.parent) == before
    assert isinstance(outcome, TableProfile)
    assert not outcome.timed_out
    assert outcome.row_count == 2
    assert [c.name for c in outcome.columns] == headers
    assert outcome.sample_columns == headers
    assert outcome.sample == [["1", "2", "3", "4", "5"], ["6", "7", "8", "9", "10"]]


def test_quote_identifier_doubles_double_quotes() -> None:
    assert quote_identifier('a"b') == '"a""b"'
    assert quote_identifier("plain") == '"plain"'


def test_the_deadline_interrupts_a_running_query() -> None:
    with locked_connection([Path.cwd()]) as connection:
        deadline = Deadline(connection, 0.1)
        started = time.monotonic()
        try:
            with pytest.raises(duckdb.InterruptException):
                connection.execute("SELECT count(*) FROM range(10000000000000)").fetchall()
        finally:
            deadline.stop()
        assert deadline.expired
        assert time.monotonic() - started < 1


def test_a_query_started_after_the_deadline_is_still_interrupted() -> None:
    """An interrupt on an idle connection is lost, so the watchdog keeps interrupting."""
    with locked_connection([Path.cwd()]) as connection:
        deadline = Deadline(connection, 0.05)
        try:
            time.sleep(0.2)
            started = time.monotonic()
            with pytest.raises(duckdb.InterruptException):
                connection.execute("SELECT count(*) FROM range(10000000000000)").fetchall()
            assert time.monotonic() - started < 1
        finally:
            deadline.stop()


def test_a_query_through_an_expired_deadline_does_not_start() -> None:
    with locked_connection([Path.cwd()]) as connection:
        deadline = Deadline(connection, 0.01)
        try:
            time.sleep(0.1)
            with pytest.raises(DeadlineExpired):
                deadline.fetchall("SELECT 1")
        finally:
            deadline.stop()


def test_a_stopped_deadline_never_interrupts() -> None:
    with locked_connection([Path.cwd()]) as connection:
        deadline = Deadline(connection, 0.05)
        assert deadline.fetchall("SELECT 1") == [(1,)]
        deadline.stop()
        time.sleep(0.1)
        assert not deadline.expired
        assert connection.execute("SELECT count(*) FROM range(1000000)").fetchall() == [(1000000,)]
