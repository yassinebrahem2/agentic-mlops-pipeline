"""A table profile as text: its exact shape, timed-out profiles and hostile text."""

from __future__ import annotations

from mlops_agents.connections.contract import TableRef
from mlops_agents.data.profile import ColumnProfile, TableProfile
from mlops_agents.data.render import (
    SIZE_CAP,
    left_out_line,
    more_tables_line,
    render_table,
    render_versioned_table,
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
        row_count=0,
        columns=[_column("id", "BIGINT"), _column("name", "VARCHAR")],
        more_columns=4,
        sample=[],
        timed_out=True,
        timeout_s=30,
    )
    assert render_versioned_table(PEOPLE, "v1", profile) == (
        'Table "people" (csv, files ["people.csv"], data version v1)\n'
        "Profile timed out after 30 s: the row count, statistics and sample rows are unknown.\n"
        "Columns:\n"
        '- "id" BIGINT\n'
        '- "name" VARCHAR\n'
        "and 4 more columns"
    )


def test_a_profile_that_timed_out_before_its_columns_shows_none() -> None:
    profile = _profile(row_count=0, columns=[], sample=[], timed_out=True, timeout_s=0.5)
    assert render_table(PEOPLE, profile) == (
        'Table "people" (csv, files ["people.csv"])\n'
        "Profile timed out after 0.5 s: the row count, statistics and sample rows are unknown."
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
