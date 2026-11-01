"""The day loop with the company in it, its CLI played by the fake script."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from demo_world import company, paths, scenarios, simulate, source
from tests.company_fixture import (
    DAILY,
    Recorder,
    add_to_schedule,
    calls,
    commands,
    fail,
    fake_company,
    job,
    make_checkout,
)
from tests.fixture import FIXTURE_CSV, FIXTURE_END, FIXTURE_START

FAST = simulate.Pace(seconds_per_day=0)
BOOTSTRAP = [
    "hotel-demo train --cutoff 2016-01-01",
    "hotel-demo evaluate --version 1",
    "hotel-demo deploy --version 1",
]


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return make_checkout(tmp_path)


def _world(
    loaded: pd.DataFrame,
    the_company: company.Company,
    scenario: scenarios.Scenario = scenarios.NO_SCENARIO,
) -> simulate.World:
    return simulate.World(
        loaded=loaded,
        data=paths.data_dir(),
        folder=paths.world_dir(),
        scenario=scenario,
        company=the_company,
    )


def _run(world: simulate.World, end: date = FIXTURE_END) -> None:
    simulate.simulate(world, FIXTURE_START, end, FAST)


def _days(start: int, end: int) -> list[str]:
    return [f"hotel-demo daily --date 2016-01-{n:02d}" for n in range(start, end + 1)]


def test_the_loop_bootstraps_once_and_runs_each_job_on_its_due_days(
    loaded: pd.DataFrame, checkout: Path
) -> None:
    add_to_schedule(checkout, job("score", "hotel-demo score --date {date}", 3))
    world = _world(loaded, fake_company(checkout))

    _run(world)
    _run(world, end=date(2016, 1, 7))

    assert commands(checkout) == [
        *BOOTSTRAP,
        "hotel-demo daily --date 2016-01-01",
        "hotel-demo score --date 2016-01-01",
        *_days(2, 3),
        "hotel-demo daily --date 2016-01-04",
        "hotel-demo score --date 2016-01-04",
        "hotel-demo daily --date 2016-01-05",
        "mlflow artifacts download",
        *_days(6, 6),
        "hotel-demo daily --date 2016-01-07",
        "hotel-demo score --date 2016-01-07",
    ]
    assert {c["HOTEL_DEMO_DATA"] for c in calls(checkout)} == {str(paths.data_dir())}
    assert {c["HOTEL_DEMO_MLFLOW"] for c in calls(checkout)} == {str(paths.mlflow_dir())}


def test_the_first_model_trains_before_the_first_days_files(
    loaded: pd.DataFrame, checkout: Path
) -> None:
    _run(_world(loaded, fake_company(checkout)))

    train = calls(checkout)[0]
    assert train["argv"][:2] == ["hotel-demo", "train"]
    assert not train["bookings_present"]
    assert not train["labels_present"]


def test_the_daily_job_sees_the_days_files(loaded: pd.DataFrame, checkout: Path) -> None:
    _run(_world(loaded, fake_company(checkout)))

    daily = [c for c in calls(checkout) if c["argv"][1] == "daily"]
    assert [c["bookings_present"] for c in daily] == [True] * 5
    assert [c["labels_present"] for c in daily] == [True] * 5


def test_the_checkout_is_updated_before_each_day(loaded: pd.DataFrame, checkout: Path) -> None:
    recorder = Recorder()

    _run(_world(loaded, fake_company(checkout, recorder)))

    daily_runs = [n for n, argv in enumerate(recorder.argvs) if "daily" in argv]
    pulls = [n for n, argv in enumerate(recorder.argvs) if argv == ["git", "pull", "--ff-only"]]
    assert len(pulls) == 5
    assert pulls[0] == 0
    assert all(pull < run for pull, run in zip(pulls, daily_runs, strict=True))
    assert all(run < pull for run, pull in zip(daily_runs, pulls[1:], strict=False))
    assert all(cwd == checkout for cwd in recorder.cwds)


def test_a_job_merged_between_days_runs_from_its_next_due_day(
    loaded: pd.DataFrame, checkout: Path
) -> None:
    pulls: list[int] = []

    def pull() -> company.Finished:
        # A merged PR lands with the third day's pull and adds a job to the schedule.
        pulls.append(1)
        if len(pulls) == 3:
            add_to_schedule(checkout, job("check", "hotel-demo score --date {date}", 2))
        return company.Finished(0, "", "")

    _run(_world(loaded, fake_company(checkout, Recorder(on_git=pull))))

    scored = [c for c in commands(checkout) if c.startswith("hotel-demo score")]
    assert scored == ["hotel-demo score --date 2016-01-03", "hotel-demo score --date 2016-01-05"]


def test_a_restart_keeps_counting_the_schedule_from_the_first_day(
    loaded: pd.DataFrame, checkout: Path
) -> None:
    world = _world(loaded, fake_company(checkout))
    _run(world, end=date(2016, 1, 3))
    add_to_schedule(checkout, job("check", "hotel-demo score --date {date}", 2))

    simulate.simulate(world, date(2016, 1, 4), date(2016, 1, 6), FAST)

    scored = [c for c in commands(checkout) if c.startswith("hotel-demo score")]
    assert scored == ["hotel-demo score --date 2016-01-05"]


def test_a_failing_daily_is_logged_and_the_next_day_runs(
    loaded: pd.DataFrame, checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fail(checkout, "hotel-demo daily --date 2016-01-02")
    world = _world(loaded, fake_company(checkout))

    _run(world)

    assert [c for c in commands(checkout) if "daily" in c] == _days(1, 5)
    assert "hotel-demo daily --date 2016-01-02" in caplog.text
    assert "exit code 3" in caplog.text
    state = json.loads((paths.world_dir() / "state.json").read_text(encoding="utf-8"))
    assert state["last_done"] == "2016-01-05"


def test_a_failing_pull_is_logged_and_the_day_goes_on(
    loaded: pd.DataFrame, checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def pull() -> company.Finished:
        return company.Finished(1, "", "fatal: unable to access the remote")

    _run(_world(loaded, fake_company(checkout, Recorder(on_git=pull))))

    assert [c for c in commands(checkout) if "daily" in c] == _days(1, 5)
    assert "unable to access the remote" in caplog.text


def test_a_score_failure_window_delivers_the_bookings_after_the_daily_job(
    loaded: pd.DataFrame, checkout: Path, tmp_path: Path
) -> None:
    scenario_file = tmp_path / "scenario.toml"
    scenario_file.write_text(
        '[[injection]]\nkind = "score_failure"\nstart = 2016-01-03\nend = 2016-01-04\n',
        encoding="utf-8",
    )
    world = _world(loaded, fake_company(checkout), scenarios.load(scenario_file))

    _run(world)

    daily = [c for c in calls(checkout) if c["argv"][1] == "daily"]
    assert [c["bookings_present"] for c in daily] == [True, True, False, False, True]
    for day in (date(2016, 1, n) for n in range(1, 6)):
        expected = source.bookings_frame(loaded, day)
        assert pq.read_table(paths.bookings_file(world.data, day)).equals(
            pa.Table.from_pandas(expected, schema=source.BOOKINGS_SCHEMA, preserve_index=False)
        )
        assert paths.labels_file(world.data, day).exists()


def test_the_schedule_file_is_the_only_list_of_jobs(loaded: pd.DataFrame, checkout: Path) -> None:
    (checkout / "ops" / "schedule.toml").write_text(DAILY.replace("daily", "score"), "utf-8")

    _run(_world(loaded, fake_company(checkout)), end=FIXTURE_START)

    assert commands(checkout) == [*BOOTSTRAP, "hotel-demo score --date 2016-01-01"]
