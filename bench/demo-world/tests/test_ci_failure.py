"""`ci_failure`: the company CI's sample data pointed at a missing file for a window of days."""

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from demo_world import answer_key, ci, ci_failure, cli, fetch, github, paths, scenarios, simulate
from demo_world import source as source_module
from tests.fake_gh import FakeGh
from tests.fixture import FIXTURE_CSV, FIXTURE_END, FIXTURE_START

REPOSITORY = "tester/hotel-cancellation-demo-ci"
MISSING = f"https://raw.githubusercontent.com/{REPOSITORY}/main/ops/missing-sample-data.tar.gz"
ORIGINAL = "https://example.org/sample-data.tar.gz"
VARIABLES = f"repos/{REPOSITORY}/actions/variables"
FAST = simulate.Pace(seconds_per_day=0)
WINDOW = '[[injection]]\nkind = "ci_failure"\nstart = 2016-01-02\nend = 2016-01-03\n'
FIVE_DAYS = ["--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "0"]


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source_module.load_source(FIXTURE_CSV)


@pytest.fixture
def gh() -> FakeGh:
    return FakeGh()


@pytest.fixture
def scenario(tmp_path: Path) -> scenarios.Scenario:
    path = tmp_path / "scenario.toml"
    path.write_text(WINDOW, encoding="utf-8")
    return scenarios.load(path)


def _clock() -> Iterator[datetime]:
    moment = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(minutes=1)


def _steps(gh: FakeGh, scenario: scenarios.Scenario) -> tuple[simulate.Step, ...]:
    repository = github.Repository(REPOSITORY, gh)
    times = _clock()
    failure = ci_failure.CiFailure(
        repository, paths.world_dir(), scenario.problems, now=lambda: next(times)
    )
    dispatcher = ci.Dispatcher(repository, paths.world_dir(), 1, sleep=lambda _: None)
    return (failure, dispatcher)


def _world(
    loaded: pd.DataFrame, steps: tuple[simulate.Step, ...], scenario: scenarios.Scenario
) -> simulate.World:
    return simulate.World(loaded, paths.data_dir(), paths.world_dir(), scenario, steps=steps)


def _events() -> list[dict[str, object]]:
    return [line for line in answer_key.lines(paths.world_dir()) if line["kind"] != "ci_failure"]


def test_an_unset_variable_is_created_for_the_window_and_deleted_after_it(
    loaded: pd.DataFrame, gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    simulate.simulate(
        _world(loaded, _steps(gh, scenario), scenario), FIXTURE_START, FIXTURE_END, FAST
    )

    assert gh.dispatched_with == [None, MISSING, MISSING, None, None]
    variable_calls = [c for c in gh.calls if "variables" in " ".join(c)]
    assert variable_calls == [
        ["api", "-X", "GET", f"{VARIABLES}/SAMPLE_DATA_URL"],
        ["api", "-X", "POST", VARIABLES, "-f", "name=SAMPLE_DATA_URL", "-f", f"value={MISSING}"],
        ["api", "-X", "DELETE", f"{VARIABLES}/SAMPLE_DATA_URL"],
    ]
    assert gh.variables == {}


def test_the_answer_key_records_the_window_in_business_dates_and_real_times(
    loaded: pd.DataFrame, gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    simulate.simulate(
        _world(loaded, _steps(gh, scenario), scenario), FIXTURE_START, FIXTURE_END, FAST
    )

    window = {"start": "2016-01-02", "end": "2016-01-03"}
    assert answer_key.lines(paths.world_dir())[0] == {
        "kind": "ci_failure",
        "category": "operational",
        "column": None,
        **window,
    }
    set_at = "2026-10-29T12:00:00+00:00"
    assert _events() == [
        {
            "kind": "ci_failure_set",
            **window,
            "date": "2016-01-02",
            "time": set_at,
            "set_at": set_at,
        },
        {
            "kind": "ci_failure_restored",
            **window,
            "date": "2016-01-04",
            "time": "2026-10-29T12:01:00+00:00",
            "set_at": set_at,
        },
    ]
    assert not (paths.world_dir() / ci_failure.MARKER_FILE).exists()


def test_a_set_variable_is_pointed_away_and_back(
    loaded: pd.DataFrame, gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    gh.variables["SAMPLE_DATA_URL"] = {"name": "SAMPLE_DATA_URL", "value": ORIGINAL}

    simulate.simulate(
        _world(loaded, _steps(gh, scenario), scenario), FIXTURE_START, FIXTURE_END, FAST
    )

    assert gh.dispatched_with == [ORIGINAL, MISSING, MISSING, ORIGINAL, ORIGINAL]
    patches = gh.named("api", "-X", "PATCH")
    assert [c[-1] for c in patches] == [f"value={MISSING}", f"value={ORIGINAL}"]
    assert gh.sample_data_url() == ORIGINAL


class _Breaks:
    """A step that fails at the end of one day, as a crash in the loop would."""

    def __init__(self, day: date, error: BaseException) -> None:
        self.day, self.error = day, error

    def start_of_day(self, day: date, origin: date) -> None:
        pass

    def end_of_day(self, day: date, origin: date) -> None:
        if day == self.day:
            raise self.error

    def close(self) -> None:
        pass


@pytest.mark.parametrize("error", [RuntimeError("the loop broke"), KeyboardInterrupt()])
def test_the_variable_is_restored_when_the_loop_stops_inside_the_window(
    loaded: pd.DataFrame, gh: FakeGh, scenario: scenarios.Scenario, error: BaseException
) -> None:
    gh.variables["SAMPLE_DATA_URL"] = {"name": "SAMPLE_DATA_URL", "value": ORIGINAL}
    steps = (*_steps(gh, scenario), _Breaks(date(2016, 1, 2), error))

    with pytest.raises(type(error)):
        simulate.simulate(_world(loaded, steps, scenario), FIXTURE_START, FIXTURE_END, FAST)

    assert gh.sample_data_url() == ORIGINAL
    assert [e["kind"] for e in _events()] == ["ci_failure_set", "ci_failure_restored"]
    assert _events()[1]["date"] == "2016-01-02"


def test_a_stop_in_the_window_breaks_it_again_on_the_resumed_days(
    loaded: pd.DataFrame, gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    world = _world(loaded, _steps(gh, scenario), scenario)
    simulate.simulate(world, FIXTURE_START, date(2016, 1, 2), FAST)
    assert gh.sample_data_url() is None

    simulate.simulate(world, FIXTURE_START, FIXTURE_END, FAST)

    assert gh.dispatched_with == [None, MISSING, MISSING, None, None]
    assert [(e["kind"], e["date"]) for e in _events()] == [
        ("ci_failure_set", "2016-01-02"),
        ("ci_failure_restored", "2016-01-02"),
        ("ci_failure_set", "2016-01-03"),
        ("ci_failure_restored", "2016-01-04"),
    ]


def test_a_run_killed_in_the_window_is_repaired_from_the_recorded_original(
    gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    gh.variables["SAMPLE_DATA_URL"] = {"name": "SAMPLE_DATA_URL", "value": ORIGINAL}
    killed, _ = _steps(gh, scenario)
    killed.start_of_day(date(2016, 1, 2), FIXTURE_START)  # never closed: a hard kill
    assert gh.sample_data_url() == MISSING

    resumed, _ = _steps(gh, scenario)
    resumed.start_of_day(date(2016, 1, 3), FIXTURE_START)
    assert gh.sample_data_url() == MISSING
    resumed.start_of_day(date(2016, 1, 4), FIXTURE_START)

    assert gh.sample_data_url() == ORIGINAL
    assert len(gh.named("api", "-X", "GET")) == 1
    assert [e["kind"] for e in _events()] == ["ci_failure_set", "ci_failure_restored"]


def test_a_failed_lookup_leaves_the_variable_alone_and_is_tried_again(
    gh: FakeGh, scenario: scenarios.Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    gh.failing_once.append(("api", "-X", "GET"))
    failure, _ = _steps(gh, scenario)

    failure.start_of_day(date(2016, 1, 2), FIXTURE_START)
    assert gh.named("api", "-X", "POST") == []
    assert "HTTP 502" in caplog.text

    failure.start_of_day(date(2016, 1, 3), FIXTURE_START)
    assert gh.sample_data_url() == MISSING


def test_a_failed_restore_is_tried_again_on_exit(
    gh: FakeGh, scenario: scenarios.Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    gh.failing_once.append(("api", "-X", "DELETE"))
    failure, _ = _steps(gh, scenario)
    failure.start_of_day(date(2016, 1, 2), FIXTURE_START)

    failure.start_of_day(date(2016, 1, 4), FIXTURE_START)
    assert gh.sample_data_url() == MISSING
    assert "HTTP 502" in caplog.text

    failure.close()
    assert gh.sample_data_url() is None
    assert [e["kind"] for e in _events()] == ["ci_failure_set", "ci_failure_restored"]


def test_a_variable_already_gone_counts_as_restored(
    gh: FakeGh, scenario: scenarios.Scenario
) -> None:
    failure, _ = _steps(gh, scenario)
    failure.start_of_day(date(2016, 1, 2), FIXTURE_START)
    del gh.variables["SAMPLE_DATA_URL"]

    failure.close()

    assert not (paths.world_dir() / ci_failure.MARKER_FILE).exists()


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_the_cli_breaks_the_runs_ci_in_the_window(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _place_source()
    (tmp_path / "scenario.toml").write_text(WINDOW, encoding="utf-8")
    monkeypatch.setattr(github, "RUNNER", gh)
    monkeypatch.setenv("HOTEL_DEMO_REPO", REPOSITORY)

    argv = [
        "simulate",
        *FIVE_DAYS,
        "--ci-every",
        "1",
        "--scenario",
        str(tmp_path / "scenario.toml"),
    ]
    assert cli.main(argv) == 0

    assert gh.dispatched_with == [None, MISSING, MISSING, None, None]


def test_without_a_repository_a_ci_failure_is_only_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _place_source()
    (tmp_path / "scenario.toml").write_text(WINDOW, encoding="utf-8")

    assert cli.main(["simulate", *FIVE_DAYS, "--scenario", str(tmp_path / "scenario.toml")]) == 0

    assert [line["kind"] for line in answer_key.lines(paths.world_dir())] == ["ci_failure"]
    assert "HOTEL_DEMO_REPO" in caplog.text
