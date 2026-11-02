from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from demo_world import scenarios, source

from benchmarks.dates import business_date
from benchmarks.jobs_days import day_starts_from_jobs

SCENARIO_PATH = Path(__file__).parents[1] / "scenarios" / "agents-short.toml"


# =============================================================================
# Short scenario tests
# =============================================================================


def test_agents_short_scenario_loads() -> None:
    """The scenario file exists and loads via demo-world's scenario parser."""
    assert SCENARIO_PATH.is_file(), f"Scenario file not found: {SCENARIO_PATH}"
    scenario = scenarios.load(SCENARIO_PATH)
    assert len(scenario.problems) == 6


def test_agents_short_scenario_structure() -> None:
    """Verify the exact windows, categories, columns, and dates of the short scenario."""
    scenario = scenarios.load(SCENARIO_PATH)

    run_start = date(2016, 7, 1)
    run_end = date(2016, 9, 11)
    quiet_end = date(2016, 7, 7)

    # 1. Quiet baseline: no problem active in the first 7 days
    curr = run_start
    while curr <= quiet_end:
        assert not any(p.active(curr) for p in scenario.problems), (
            f"Problem active on quiet day {curr}"
        )
        curr += timedelta(days=1)

    # 2. Every window falls strictly inside the run dates
    for problem in scenario.problems:
        assert run_start <= problem.start <= run_end
        assert run_start <= problem.end <= run_end
        # Each window is exactly 3 days (inclusive)
        assert (problem.end - problem.start).days + 1 == 3

    # 3. Exactly six windows spaced 9 days apart
    windows = [(p.start, p.end) for p in scenario.problems]
    expected_starts = [
        date(2016, 7, 8),
        date(2016, 7, 19),
        date(2016, 7, 30),
        date(2016, 8, 10),
        date(2016, 8, 21),
        date(2016, 9, 1),
    ]
    assert [w[0] for w in windows] == expected_starts

    for i in range(len(windows) - 1):
        prev_end = windows[i][1]
        next_start = windows[i + 1][0]
        assert (next_start - prev_end).days == 9

    # Last window ends 2016-09-03, run ends 8 days later on 2016-09-11
    assert windows[-1][1] == date(2016, 9, 3)
    assert (run_end - windows[-1][1]).days == 8

    # 4. Five data_quality problems, one score_failure, no schema_break
    kinds = [p.kind for p in scenario.problems]
    assert "schema_break" not in kinds
    assert kinds.count("score_failure") == 1
    assert kinds.count("null_spike") == 4
    assert kinds.count("out_of_range") == 1

    # Check categories
    categories = [scenarios.CATEGORIES[p.kind] for p in scenario.problems]
    assert categories.count("data_quality") == 5
    assert categories.count("operational") == 1

    # 5. Distinct columns for data_quality, all existing in BOOKINGS_SCHEMA
    schema_names: list[str] = cast(
        list[str],
        source.BOOKINGS_SCHEMA.names,  # pyright: ignore[reportUnknownMemberType]  # pyarrow Schema boundary
    )
    known_columns: set[str] = set(schema_names)
    dq_columns: list[str] = []
    for p in scenario.problems:
        if scenarios.CATEGORIES[p.kind] == "data_quality":
            assert len(p.columns) == 1
            col = p.columns[0]
            assert col in known_columns
            dq_columns.append(col)

    assert len(set(dq_columns)) == 5, f"Columns are not distinct: {dq_columns}"


# =============================================================================
# day_starts_from_jobs tests
# =============================================================================


def test_day_starts_from_jobs_basic(tmp_path: Path) -> None:
    """Fixture folder with valid jsonl files gives earliest start per day in UTC."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    # Day 1: two lines out of order
    day1 = date(2016, 7, 1)
    day1_file = jobs_dir / f"{day1.isoformat()}.jsonl"
    day1_file.write_text(
        json.dumps({"job": "daily", "started": "2016-07-01T10:30:00+00:00"})
        + "\n"
        + json.dumps({"job": "score", "started": "2016-07-01T09:15:00+00:00"})
        + "\n",
        encoding="utf-8",
    )

    # Day 2: single line with Z timezone
    day2 = date(2016, 7, 2)
    day2_file = jobs_dir / f"{day2.isoformat()}.jsonl"
    day2_file.write_text(
        json.dumps({"job": "daily", "started": "2016-07-02T08:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    starts, gaps = day_starts_from_jobs(jobs_dir)

    assert gaps == []
    assert len(starts) == 2
    assert starts[0] == (day1, datetime(2016, 7, 1, 9, 15, 0, tzinfo=UTC))
    assert starts[1] == (day2, datetime(2016, 7, 2, 8, 0, 0, tzinfo=UTC))


def test_day_starts_from_jobs_missing_file_as_gap(tmp_path: Path) -> None:
    """Missing file within the evaluated date range is reported as a gap."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    day1 = date(2016, 7, 1)
    day3 = date(2016, 7, 3)

    (jobs_dir / f"{day1.isoformat()}.jsonl").write_text(
        json.dumps({"started": "2016-07-01T09:00:00Z"}) + "\n", encoding="utf-8"
    )
    # Day 2 is missing
    (jobs_dir / f"{day3.isoformat()}.jsonl").write_text(
        json.dumps({"started": "2016-07-03T09:00:00Z"}) + "\n", encoding="utf-8"
    )

    starts, gaps = day_starts_from_jobs(jobs_dir)

    assert gaps == [date(2016, 7, 2)]
    assert [d for d, _ in starts] == [day1, day3]


def test_day_starts_from_jobs_empty_file_as_gap(tmp_path: Path) -> None:
    """An empty file or a file with no valid started line is reported as a gap."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    day1 = date(2016, 7, 1)
    day2 = date(2016, 7, 2)
    day3 = date(2016, 7, 3)

    (jobs_dir / f"{day1.isoformat()}.jsonl").write_text(
        json.dumps({"started": "2016-07-01T09:00:00Z"}) + "\n", encoding="utf-8"
    )
    # Day 2 is an empty file
    (jobs_dir / f"{day2.isoformat()}.jsonl").write_text("", encoding="utf-8")
    # Day 3 has json without started or invalid json
    (jobs_dir / f"{day3.isoformat()}.jsonl").write_text(
        "not json\n" + json.dumps({"other": "field"}) + "\n", encoding="utf-8"
    )

    starts, gaps = day_starts_from_jobs(jobs_dir)

    assert gaps == [day2, day3]
    assert [d for d, _ in starts] == [day1]


def test_day_starts_from_jobs_replayed_day(tmp_path: Path) -> None:
    """A replayed day (lines appended twice) still returns the earliest start."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    day = date(2016, 7, 1)
    line1 = json.dumps({"job": "daily", "started": "2016-07-01T09:00:00Z"}) + "\n"
    line2 = json.dumps({"job": "score", "started": "2016-07-01T09:10:00Z"}) + "\n"

    # Appended twice
    (jobs_dir / f"{day.isoformat()}.jsonl").write_text(
        line1 + line2 + line1 + line2, encoding="utf-8"
    )

    starts, gaps = day_starts_from_jobs(jobs_dir)

    assert gaps == []
    assert len(starts) == 1
    assert starts[0] == (day, datetime(2016, 7, 1, 9, 0, 0, tzinfo=UTC))


def test_day_starts_from_jobs_explicit_date_range(tmp_path: Path) -> None:
    """When date_range is passed, only that range is evaluated and missing dates are gaps."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    (jobs_dir / "2016-07-01.jsonl").write_text(
        json.dumps({"started": "2016-07-01T09:00:00Z"}) + "\n", encoding="utf-8"
    )
    (jobs_dir / "2016-07-02.jsonl").write_text(
        json.dumps({"started": "2016-07-02T09:00:00Z"}) + "\n", encoding="utf-8"
    )
    (jobs_dir / "2016-07-05.jsonl").write_text(
        json.dumps({"started": "2016-07-05T09:00:00Z"}) + "\n", encoding="utf-8"
    )

    # Evaluate range 2016-07-02 to 2016-07-04
    starts, gaps = day_starts_from_jobs(jobs_dir, date_range=(date(2016, 7, 2), date(2016, 7, 4)))

    assert [d for d, _ in starts] == [date(2016, 7, 2)]
    assert gaps == [date(2016, 7, 3), date(2016, 7, 4)]


def test_day_starts_from_jobs_empty_directory(tmp_path: Path) -> None:
    """Empty or non-existent folder returns empty starts and gaps when date_range is None."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    starts, gaps = day_starts_from_jobs(empty_dir)
    assert starts == []
    assert gaps == []

    non_existent = tmp_path / "missing"
    starts, gaps = day_starts_from_jobs(non_existent)
    assert starts == []
    assert gaps == []


def test_gap_day_falls_back_to_previous_start_in_business_date(tmp_path: Path) -> None:
    """Business_date(at, starts) falls back to previous day's start during a gap day."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()

    # Day 1: 2016-07-01 starts at 09:00 UTC
    (jobs_dir / "2016-07-01.jsonl").write_text(
        json.dumps({"started": "2016-07-01T09:00:00Z"}) + "\n", encoding="utf-8"
    )
    # Day 2: 2016-07-02 is a gap
    # Day 3: 2016-07-03 starts at 09:00 UTC
    (jobs_dir / "2016-07-03.jsonl").write_text(
        json.dumps({"started": "2016-07-03T09:00:00Z"}) + "\n", encoding="utf-8"
    )

    starts, gaps = day_starts_from_jobs(jobs_dir)
    assert gaps == [date(2016, 7, 2)]

    # At 2016-07-02 12:00 UTC (during gap day 2), business_date falls back to day 1
    at_gap = datetime(2016, 7, 2, 12, 0, tzinfo=UTC)
    assert business_date(at_gap, starts) == date(2016, 7, 1)

    # At 2016-07-03 09:00 UTC, business_date is day 3
    at_day3 = datetime(2016, 7, 3, 9, 0, tzinfo=UTC)
    assert business_date(at_day3, starts) == date(2016, 7, 3)
