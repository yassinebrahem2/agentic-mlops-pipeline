"""The world against the real company: a checkout of the demo repository's `main`.

These run only when HOTEL_DEMO_COMPANY names that checkout, with its environment synced
(`uv sync` there): the CI's contract job does, and a developer can. Each trains a real model,
so they are slow.
"""

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from demo_world import backfill, company, paths, scenarios, simulate, source
from tests.fixture import FIXTURE_CSV, FIXTURE_END, FIXTURE_START

# Read at import: the autouse fixture removes the variable from every test's environment.
CHECKOUT = os.environ.get("HOTEL_DEMO_COMPANY")

pytestmark = [
    pytest.mark.skipif(CHECKOUT is None, reason="HOTEL_DEMO_COMPANY names no company checkout"),
    pytest.mark.timeout(900),
]

FIVE_DAYS = [date(2016, 1, n) for n in range(1, 6)]


def _play(scenario: scenarios.Scenario) -> Path:
    assert CHECKOUT is not None
    loaded = source.load_source(FIXTURE_CSV)
    data = paths.data_dir()
    backfill.backfill(loaded, data, FIXTURE_START)
    the_company = company.Company.at(Path(CHECKOUT), data, paths.mlflow_dir())
    world = simulate.World(loaded, data, paths.world_dir(), scenario, the_company)
    simulate.simulate(world, FIXTURE_START, FIXTURE_END, simulate.Pace(seconds_per_day=0))
    return data


def _jobs(data: Path, day: date) -> list[dict[str, object]]:
    path = data / "jobs" / f"{day.isoformat()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_company_runs_its_daily_job_on_the_worlds_days() -> None:
    """The contract test: five days with no scenario, the company's real `daily` on each."""
    data = _play(scenarios.NO_SCENARIO)

    for day in FIVE_DAYS:
        jobs = _jobs(data, day)
        assert jobs[0]["job"] == "score"
        assert [job for job in jobs if job["status"] != "success"] == []
        predictions = pd.read_parquet(data / "predictions" / f"{day.isoformat()}.parquet")
        bookings = pd.read_parquet(paths.bookings_file(data, day))
        assert sorted(predictions["booking_id"]) == sorted(bookings["booking_id"])


def test_a_score_failure_window_makes_the_real_daily_job_fail(tmp_path: Path) -> None:
    scenario_file = tmp_path / "scenario.toml"
    scenario_file.write_text(
        '[[injection]]\nkind = "score_failure"\nstart = 2016-01-03\nend = 2016-01-03\n',
        encoding="utf-8",
    )

    data = _play(scenarios.load(scenario_file))

    (score,) = [job for job in _jobs(data, date(2016, 1, 3)) if job["job"] == "score"]
    assert score["status"] == "failure"
    assert "Bookings file not found" in str(score["error"])
    assert not (data / "predictions" / "2016-01-03.parquet").exists()
    assert paths.bookings_file(data, date(2016, 1, 3)).exists()
    for day in (date(2016, 1, 2), date(2016, 1, 4)):
        assert {job["status"] for job in _jobs(data, day)} == {"success"}
