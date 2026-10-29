"""A table profile as text: its exact shape, timed-out profiles and hostile text."""

from __future__ import annotations

from pathlib import Path

from mlops_agents.connections.contract import ConnectionView, Findings, TableRef
from mlops_agents.data.constants import LISTED_FILES, LISTED_TABLE_NAMES, TEXT_CUT
from mlops_agents.data.profile import ColumnProfile, TableProfile
from mlops_agents.data.render import (
    SIZE_CAP,
    left_out_entries_line,
    left_out_line,
    more_tables_line,
    reason_without_folders,
    render_table,
    render_versioned_table,
    unreachable_line,
    unreadable_line,
)

PEOPLE = TableRef(name="people", files=["people.csv"], format="csv")


def _column(name: str, type_: str, **stats: object) -> ColumnProfile:
    values: dict[str, object] = {
        "nulls": 0,
        "distinct": 0,
        "min": None,
        "max": None,
        "top_values": [],
    }
    values.update(stats)
    return ColumnProfile.model_validate({"name": name, "type": type_, **values})


def _profile(**changes: object) -> TableProfile:
    values: dict[str, object] = {
        "row_count": 3,
        "columns": [
            _column("id", "BIGINT", distinct=3, min="1", max="3"),
            _column("name", "VARCHAR", nulls=1, distinct=2, top_values=["ann", "bob"]),
            _column("flag", "BOOLEAN", distinct=2),
        ],
        "more_columns": 0,
        "sample_columns": ["id", "name", "flag"],
        "sample": [["1", "ann", "true"], ["2", "bob", "false"], ["3", "NULL", "true"]],
        "timed_out": False,
        "incomplete": False,
        "timeout_s": 30,
    }
    values.update(changes)
    return TableProfile.model_validate(values)


def test_a_table_renders_its_header_columns_and_sample() -> None:
    assert render_table(PEOPLE, _profile()) == (
        'Table "people" (csv, files ["people.csv"]): 3 rows\n'
        "Columns:\n"
        '- "id" BIGINT, nulls 0, ~distinct 3, 1..3\n'
        '- "name" VARCHAR, nulls 1, ~distinct 2, top ["ann", "bob"]\n'
        '- "flag" BOOLEAN, nulls 0, ~distinct 2\n'
        "Sample rows (first 3):\n"
        '["1", "ann", "true"]\n'
        '["2", "bob", "false"]\n'
        '["3", "NULL", "true"]'
    )


def test_the_versioned_form_adds_the_data_version_to_the_header() -> None:
    text = render_versioned_table(PEOPLE, "0123abcd", _profile())
    first, rest = text.split("\n", 1)
    assert first == 'Table "people" (csv, files ["people.csv"], data version 0123abcd): 3 rows'
    assert rest == render_table(PEOPLE, _profile()).split("\n", 1)[1]


def test_the_columns_past_the_cap_are_counted() -> None:
    text = render_table(PEOPLE, _profile(more_columns=10))
    assert '- "flag" BOOLEAN, nulls 0, ~distinct 2\nand 10 more columns\nSample rows' in text


def test_an_empty_table_says_it_has_no_sample_rows() -> None:
    text = render_table(PEOPLE, _profile(row_count=0, sample=[]))
    assert text.startswith('Table "people" (csv, files ["people.csv"]): 0 rows\n')
    assert text.endswith("\nSample rows: none")


def test_a_timed_out_profile_shows_only_names_and_types() -> None:
    profile = _profile(
        row_count=None,
        columns=[_column("id", "BIGINT"), _column("name", "VARCHAR")],
        more_columns=4,
        sample=[],
        timed_out=True,
        timeout_s=30,
    )
    assert render_versioned_table(PEOPLE, "v1", profile) == (
        'Table "people" (csv, files ["people.csv"], data version v1): '
        "row count unknown (timed out)\n"
        "Profile timed out after 30 s: the statistics and sample rows are unknown.\n"
        "Columns:\n"
        '- "id" BIGINT\n'
        '- "name" VARCHAR\n'
        "and 4 more columns"
    )


def test_a_timed_out_profile_whose_count_finished_shows_it() -> None:
    profile = _profile(
        row_count=3, columns=[_column("id", "BIGINT")], sample=[], timed_out=True, timeout_s=30
    )
    assert render_table(PEOPLE, profile).split("\n")[:2] == [
        'Table "people" (csv, files ["people.csv"]): 3 rows',
        "Profile timed out after 30 s: the statistics and sample rows are unknown.",
    ]


def test_an_incomplete_profile_shows_what_finished_and_says_the_rest_is_unknown() -> None:
    """Out of memory after the statistics (Decision #432): they are facts, the sample is not."""
    profile = _profile(sample_columns=[], sample=[], incomplete=True)
    assert render_versioned_table(PEOPLE, "v1", profile) == (
        'Table "people" (csv, files ["people.csv"], data version v1): 3 rows\n'
        "Profile incomplete (memory limit): anything not shown below is unknown.\n"
        "Columns:\n"
        '- "id" BIGINT, nulls 0, ~distinct 3, 1..3\n'
        '- "name" VARCHAR, nulls 1, ~distinct 2, top ["ann", "bob"]\n'
        '- "flag" BOOLEAN, nulls 0, ~distinct 2\n'
        "Sample rows: unknown"
    )


def test_an_incomplete_profile_without_its_statistics_shows_only_names_and_types() -> None:
    profile = _profile(
        row_count=None,
        columns=[_column("id", "BIGINT"), _column("name", "VARCHAR")],
        sample_columns=[],
        sample=[],
        incomplete=True,
    )
    assert render_table(PEOPLE, profile) == (
        'Table "people" (csv, files ["people.csv"]): row count unknown (memory limit)\n'
        "Profile incomplete (memory limit): anything not shown below is unknown.\n"
        "Columns:\n"
        '- "id" BIGINT\n'
        '- "name" VARCHAR\n'
        "Sample rows: unknown"
    )


def test_one_row_is_singular() -> None:
    profile = _profile(row_count=1, sample=[["1", "ann", "true"]])
    assert render_table(PEOPLE, profile).startswith(
        'Table "people" (csv, files ["people.csv"]): 1 row\n'
    )


def test_an_unreadable_reason_is_cut_to_its_first_line_and_twice_the_text_cut() -> None:
    reason = "x" * (TEXT_CUT * 3) + "\nOriginal Line: secret,row\nfile = elsewhere"
    line = unreadable_line(PEOPLE, reason)
    assert line == f'Table "people" (csv): unreadable: {"x" * (TEXT_CUT * 2)}'
    assert unreadable_line(PEOPLE, "short\r\nsecond") == ('Table "people" (csv): unreadable: short')


# DuckDB 2.0.0's text for a Latin-1 export and for a UTF-16 one, without the settings block.
LATIN1_REASON = (
    "Invalid Input Error: CSV Error on Line: 2\n"
    "Original Line: id;nom;ville\n"
    "Invalid unicode (byte sequence mismatch) detected. This file is not utf-8 encoded.\n"
    "\n"
    "Possible Solution: Set the correct encoding, if available, to read this CSV File\n"
    "  file = /srv/team exports/clients.csv"
)
UTF16_REASON = (
    "Invalid Input Error: CSV Error on Line: 1\n"
    "Invalid unicode (byte sequence mismatch) detected. This file is not utf-8 encoded.\n"
)
CSV_CAUSE = (
    "Invalid Input Error: CSV Error on Line: 2: "
    "Invalid unicode (byte sequence mismatch) detected. This file is not utf-8 encoded."
)


def test_a_csv_error_keeps_the_sentence_naming_its_cause() -> None:
    """DuckDB's first line gives only the place; the cause follows the copy of the team's row,
    which is never shown (Decision #431)."""
    assert unreadable_line(PEOPLE, LATIN1_REASON) == (
        f'Table "people" (csv): unreadable: {CSV_CAUSE}'
    )
    assert unreadable_line(PEOPLE, UTF16_REASON) == (
        f'Table "people" (csv): unreadable: {CSV_CAUSE.replace("Line: 2", "Line: 1")}'
    )


def test_a_row_copied_over_several_lines_is_not_taken_for_the_cause() -> None:
    """A quoted cell can hold a line break, so the copy of the row can span lines: the cause
    is the line before DuckDB's advice."""
    reason = (
        "Invalid Input Error: CSV Error on Line: 30002\n"
        'Original Line: 1,"multi\n'
        'line",3\n'
        "Expected Number of Columns: 2 Found: 3\n"
        "Possible fixes:\n"
        "* Enable ignore errors (ignore_errors=true) to skip this row\n"
    )
    assert unreadable_line(PEOPLE, reason) == (
        'Table "people" (csv): unreadable: Invalid Input Error: CSV Error on Line: 30002: '
        "Expected Number of Columns: 2 Found: 3"
    )


def test_a_csv_error_without_a_cause_keeps_its_first_line() -> None:
    expected = 'Table "people" (csv): unreadable: Invalid Input Error: CSV Error on Line: 7'
    for rest in ("Original Line: a,b,c\n\n", 'Original Line: "a\n\nb",c\n\nPossible fixes:'):
        reason = f"Invalid Input Error: CSV Error on Line: 7\n{rest}"
        assert unreadable_line(PEOPLE, reason) == expected


def test_an_unreachable_detail_is_cut_like_a_reason() -> None:
    view = ConnectionView(
        id="con_x",
        name="gone",
        kind="local_folder",
        settings={},
        status="unreachable",
        status_detail=None,
        findings=Findings(unsupported_files=[], secret_files=[], truncated=False),
        data_version=None,
    )
    detail = "y" * (TEXT_CUT * 3) + "\nsecond line"
    assert unreachable_line(view, detail) == (
        f'Connection "gone" (local_folder, unreachable): {"y" * (TEXT_CUT * 2)}'
    )
    assert unreachable_line(view, None) == 'Connection "gone" (local_folder, unreachable): '


def test_the_connections_folder_becomes_its_mount_path_in_a_reason() -> None:
    folder = Path("/srv/team exports")
    reason = f'Error when sniffing file "{folder.as_posix()}/clients.csv".'
    assert reason_without_folders(reason, [folder], "/data/exports") == (
        'Error when sniffing file "/data/exports/clients.csv".'
    )


def test_a_typed_folder_is_replaced_as_it_was_typed() -> None:
    """A folder quoted as the user typed it, mixed slashes included."""
    typed = "C:\\Users\\ann/team exports"
    reason = f"Folder does not exist: {typed}"
    assert reason_without_folders(reason, [typed], "/data/exports") == (
        "Folder does not exist: /data/exports"
    )


def test_the_counted_line_names_the_entries_left_out() -> None:
    assert left_out_entries_line(7) == "and 7 more entries left out: size cap"


def test_a_profile_that_timed_out_before_its_columns_shows_none() -> None:
    profile = _profile(row_count=None, columns=[], sample=[], timed_out=True, timeout_s=0.5)
    assert render_table(PEOPLE, profile) == (
        'Table "people" (csv, files ["people.csv"]): row count unknown (timed out)\n'
        "Profile timed out after 0.5 s: the statistics and sample rows are unknown."
    )


def test_a_table_lists_its_first_files_and_counts_the_rest() -> None:
    files = [f"events/part-{i:05}.csv" for i in range(LISTED_FILES + 7)]
    ref = TableRef(name="events", files=files, format="csv")
    first = render_table(ref, _profile()).split("\n", 1)[0]
    listed = ", ".join(f'"{name}"' for name in files[:LISTED_FILES])
    assert first == f'Table "events" (csv, files [{listed}] and 7 more files): 3 rows'


def test_a_table_with_exactly_the_listed_files_counts_none() -> None:
    files = [f"events/part-{i:05}.csv" for i in range(LISTED_FILES)]
    ref = TableRef(name="events", files=files, format="csv")
    first = render_table(ref, _profile()).split("\n", 1)[0]
    listed = ", ".join(f'"{name}"' for name in files)
    assert first == f'Table "events" (csv, files [{listed}]): 3 rows'


def test_the_more_tables_line_names_the_first_tables_and_counts_the_rest() -> None:
    refs = [TableRef(name=f"t{i}", files=[f"t{i}.csv"], format="csv") for i in range(30)]
    names = ", ".join(f'"t{i}"' for i in range(LISTED_TABLE_NAMES))
    assert more_tables_line(refs) == (
        f"and 30 more tables: {names}, and {30 - LISTED_TABLE_NAMES} more"
    )
    assert more_tables_line(refs[:LISTED_TABLE_NAMES]) == (
        f"and {LISTED_TABLE_NAMES} more tables: {names}"
    )


def test_hostile_names_and_cells_stay_on_their_own_lines() -> None:
    hostile = 'a"b\nc\r\N{LINE SEPARATOR}d\\e\x85'
    ref = TableRef(name=hostile, files=[f"{hostile}.csv"], format="csv")
    profile = _profile(
        columns=[_column(hostile, "VARCHAR", top_values=[hostile])],
        sample_columns=[hostile],
        sample=[[hostile], ["plain"]],
    )
    text = render_table(ref, profile)
    lines = text.split("\n")
    assert len(lines) == 6
    assert "\r" not in text
    assert "\N{LINE SEPARATOR}" not in text
    assert "\x85" not in text
    escaped = r'"a\"b\nc\r\u2028d\\e\u0085"'
    assert lines[0].startswith(f"Table {escaped} (csv, files [")
    assert lines[2] == f"- {escaped} VARCHAR, nulls 0, ~distinct 0, top [{escaped}]"
    assert lines[4:] == [f"[{escaped}]", '["plain"]']


def test_lone_surrogates_are_escaped_so_the_text_encodes_as_utf8() -> None:
    """A name whose bytes are not UTF-8 (Linux) or with an unpaired surrogate (Windows)."""
    odd = "a" + chr(0xD800) + "b" + chr(0xDCFF)
    ref = TableRef(name=odd, files=[f"{odd}.csv"], format="csv")
    profile = _profile(
        columns=[_column(odd, "VARCHAR", top_values=[odd])], sample_columns=[odd], sample=[[odd]]
    )
    texts = [
        render_table(ref, profile),
        render_versioned_table(ref, odd, profile),
        left_out_line(ref, SIZE_CAP),
        unreadable_line(ref, odd),
        more_tables_line([PEOPLE, ref]),
    ]
    escaped = "a" + "\\" + "ud800b" + "\\" + "udcff"
    for text in texts:
        text.encode("utf-8")
        assert escaped in text


def test_range_values_are_one_line_each() -> None:
    column = _column("t", "TIMESTAMP", distinct=2, min="2024-01-01\n00:00", max="2024-02-01")
    text = render_table(PEOPLE, _profile(columns=[column], sample_columns=["t"], sample=[]))
    assert '- "t" TIMESTAMP, nulls 0, ~distinct 2, 2024-01-01\\n00:00..2024-02-01' in text
