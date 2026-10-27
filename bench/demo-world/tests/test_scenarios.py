from datetime import date
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from demo_world import scenarios, source
from demo_world.answer_key import Record
from tests.fixture import DEFAULT_SCENARIO, FIXTURE_CSV

DAY = date(2016, 1, 2)
WINDOW = "start = 2016-01-02\nend = 2016-01-03\n"


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


def _load(tmp_path: Path, text: str) -> scenarios.Scenario:
    path = tmp_path / "scenario.toml"
    path.write_text(text, encoding="utf-8")
    return scenarios.load(path)


def _injection(body: str) -> str:
    return f"[[injection]]\n{body}{WINDOW}"


def _bookings(loaded: pd.DataFrame, day: date = DAY) -> pd.DataFrame:
    return source.bookings_frame(loaded, day)


def test_the_default_scenario_has_one_of_each_kind_and_the_natural_shift() -> None:
    scenario = scenarios.load(DEFAULT_SCENARIO)

    kinds = [problem.kind for problem in scenario.problems]
    assert sorted(kinds) == sorted([*scenarios.INJECTED_KINDS, "natural_shift"])
    shift = next(p for p in scenario.problems if p.kind == "natural_shift")
    assert (shift.start, shift.end) == (date(2016, 11, 1), date(2016, 12, 31))
    assert shift.columns == ("deposit_type", "market_segment")
    spike = next(p for p in scenario.problems if p.kind == "null_spike")
    assert spike.columns == ("country", "adr")


def test_the_default_injections_avoid_january_the_natural_shift_and_each_other() -> None:
    injected = [p for p in scenarios.load(DEFAULT_SCENARIO).problems if p.kind != "natural_shift"]

    for problem in injected:
        assert date(2016, 2, 1) <= problem.start <= problem.end < date(2016, 11, 1)
    windows = sorted((p.start, p.end) for p in injected)
    # Apart by more than the legacy checks' 30-day lookback, so each is scored on its own.
    assert all((nxt - end).days > 30 for (_, end), (nxt, _) in pairwise(windows))


def test_a_scenario_lists_one_answer_key_record_per_column(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        _injection('kind = "null_spike"\ncolumns = ["country", "adr"]\nshare = 0.5\n')
        + _injection('kind = "ci_failure"\n')
        + "[[natural_shift]]\ncolumns = ['deposit_type']\nstart = 2016-11-01\nend = 2016-12-31\n",
    )

    assert scenario.key_records(DAY) == [
        Record("null_spike", "data_quality", "country", "2016-01-02", "2016-01-03"),
        Record("null_spike", "data_quality", "adr", "2016-01-02", "2016-01-03"),
        Record("ci_failure", "operational", None, "2016-01-02", "2016-01-03"),
    ]
    assert scenario.key_records(date(2016, 1, 4)) == []
    assert scenario.key_records(date(2016, 12, 31)) == [
        Record("natural_shift", "drift", "deposit_type", "2016-11-01", "2016-12-31")
    ]


def test_no_scenario_injects_nothing(loaded: pd.DataFrame) -> None:
    frame, schema = scenarios.NO_SCENARIO.inject(_bookings(loaded), DAY)

    assert frame.equals(_bookings(loaded))
    assert schema == source.BOOKINGS_SCHEMA
    assert scenarios.NO_SCENARIO.key_records(DAY) == []


def test_null_spike_nulls_a_share_of_rows_in_each_column(
    tmp_path: Path, loaded: pd.DataFrame
) -> None:
    scenario = _load(
        tmp_path, _injection('kind = "null_spike"\ncolumns = ["country", "adr"]\nshare = 0.5\n')
    )

    frame, schema = scenario.inject(_bookings(loaded), DAY)

    assert frame["country"].isna().sum() == 1
    assert frame["adr"].isna().sum() == 1
    assert schema.field("adr").nullable
    assert schema.field("adr").type == pa.float64()
    assert schema.remove(schema.get_field_index("adr")) == source.BOOKINGS_SCHEMA.remove(
        source.BOOKINGS_SCHEMA.get_field_index("adr")
    )
    untouched = _bookings(loaded).drop(columns=["country", "adr"])
    assert frame.drop(columns=["country", "adr"]).equals(untouched)


def test_null_spike_rounds_a_share_up_so_a_small_day_is_still_hit(
    tmp_path: Path, loaded: pd.DataFrame
) -> None:
    scenario = _load(tmp_path, _injection('kind = "null_spike"\ncolumns = ["adr"]\nshare = 0.1\n'))

    frame, _ = scenario.inject(_bookings(loaded), DAY)

    assert frame["adr"].isna().sum() == 1


def test_null_spike_picks_the_same_rows_on_every_run(tmp_path: Path, loaded: pd.DataFrame) -> None:
    scenario = _load(
        tmp_path, _injection('kind = "null_spike"\ncolumns = ["country"]\nshare = 0.5\n')
    )

    first, _ = scenario.inject(_bookings(loaded), DAY)
    again, _ = _load(
        tmp_path, _injection('kind = "null_spike"\ncolumns = ["country"]\nshare = 0.5\n')
    ).inject(_bookings(loaded), DAY)

    assert first.equals(again)


def test_schema_break_renames_a_column(tmp_path: Path, loaded: pd.DataFrame) -> None:
    scenario = _load(
        tmp_path,
        _injection('kind = "schema_break"\ncolumn = "lead_time"\nrename_to = "lead_days"\n'),
    )

    frame, schema = scenario.inject(_bookings(loaded), DAY)

    assert "lead_time" not in frame.columns
    assert frame["lead_days"].tolist() == _bookings(loaded)["lead_time"].tolist()
    assert schema.names == [
        "lead_days" if name == "lead_time" else name for name in source.BOOKINGS_SCHEMA.names
    ]
    assert schema.field("lead_days").type == pa.int64()


def test_schema_break_drops_a_column(tmp_path: Path, loaded: pd.DataFrame) -> None:
    scenario = _load(
        tmp_path, _injection('kind = "schema_break"\ncolumn = "deposit_type"\ndrop = true\n')
    )

    frame, schema = scenario.inject(_bookings(loaded), DAY)

    assert "deposit_type" not in frame.columns
    assert "deposit_type" not in schema.names
    assert len(schema) == len(source.BOOKINGS_SCHEMA) - 1


def test_out_of_range_multiplies_a_column(tmp_path: Path, loaded: pd.DataFrame) -> None:
    scenario = _load(
        tmp_path, _injection('kind = "out_of_range"\ncolumn = "adr"\nfactor = 100.0\n')
    )

    frame, schema = scenario.inject(_bookings(loaded), DAY)

    assert frame["adr"].tolist() == [a * 100 for a in _bookings(loaded)["adr"]]
    assert schema == source.BOOKINGS_SCHEMA


@pytest.mark.parametrize("kind", ["score_failure", "ci_failure"])
def test_the_failures_change_no_bookings_here(
    tmp_path: Path, loaded: pd.DataFrame, kind: str
) -> None:
    scenario = _load(tmp_path, _injection(f'kind = "{kind}"\n'))

    frame, schema = scenario.inject(_bookings(loaded), DAY)

    assert frame.equals(_bookings(loaded))
    assert schema == source.BOOKINGS_SCHEMA


def test_an_injection_changes_nothing_outside_its_window(
    tmp_path: Path, loaded: pd.DataFrame
) -> None:
    scenario = _load(tmp_path, _injection('kind = "out_of_range"\ncolumn = "adr"\nfactor = 10.0\n'))
    day = date(2016, 1, 4)

    frame, schema = scenario.inject(_bookings(loaded, day), day)

    assert frame.equals(_bookings(loaded, day))
    assert schema == source.BOOKINGS_SCHEMA


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_injection('kind = "flood"\n'), "unknown kind 'flood'"),
        (
            '[[injection]]\nkind = "ci_failure"\nstart = 2016-02-02\nend = 2016-02-01\n',
            "ends before it starts",
        ),
        ('[[injection]]\nkind = "ci_failure"\nstart = 2016-02-02\n', "missing 'end'"),
        (
            _injection('kind = "null_spike"\ncolumns = ["room"]\nshare = 0.5\n'),
            "unknown column 'room'",
        ),
        (_injection('kind = "null_spike"\ncolumns = ["adr"]\nshare = 1.5\n'), "share"),
        (_injection('kind = "null_spike"\ncolumns = ["adr"]\nshare = 0\n'), "share"),
        (_injection('kind = "null_spike"\ncolumns = []\nshare = 0.5\n'), "columns"),
        (_injection('kind = "null_spike"\ncolumns = ["adr"]\n'), "missing 'share'"),
        (_injection('kind = "ci_failure"\ncolumn = "adr"\n'), "unexpected 'column'"),
        (_injection('kind = "schema_break"\ncolumn = "adr"\n'), "exactly one of"),
        (
            _injection('kind = "schema_break"\ncolumn = "adr"\nrename_to = "hotel"\n'),
            "already a column",
        ),
        (_injection('kind = "out_of_range"\ncolumn = "hotel"\nfactor = 2.0\n'), "float"),
        (_injection('kind = "out_of_range"\ncolumn = "adr"\nfactor = 1.0\n'), "factor"),
        (
            '[[injection]]\nkind = "ci_failure"\nstart = 2016-02-01T08:00:00\nend = 2016-02-02\n',
            "a date",
        ),
        ('[[incident]]\nkind = "ci_failure"\n', "unexpected 'incident'"),
        (
            _injection('kind = "out_of_range"\ncolumn = "adr"\nfactor = 2.0\n')
            + _injection('kind = "null_spike"\ncolumns = ["adr"]\nshare = 0.5\n'),
            "overlap",
        ),
        ("[[injection]\n", "not valid TOML"),
    ],
)
def test_a_bad_scenario_fails_with_a_clear_error(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(scenarios.ScenarioError, match=message) as raised:
        _load(tmp_path, text)

    assert "scenario.toml" in str(raised.value)
