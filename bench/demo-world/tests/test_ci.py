"""CI at simulation speed: the scheduled workflow dispatched every N simulated days."""

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from demo_world import ci, cli, fetch, github, paths, simulate, source
from tests.fake_gh import FakeGh
from tests.fixture import FIXTURE_CSV, FIXTURE_START

REPOSITORY = "tester/hotel-cancellation-demo-ci"
FAST = simulate.Pace(seconds_per_day=0)
NOON = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
FIVE_DAYS = ["--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "0"]
DISPATCH = [
    "api",
    "-X",
    "POST",
    f"repos/{REPOSITORY}/actions/workflows/scheduled.yml/dispatches",
    "-f",
    "ref=main",
]


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


@pytest.fixture
def gh() -> FakeGh:
    return FakeGh()


def _dispatcher(gh: FakeGh, every: int) -> ci.Dispatcher:
    repository = github.Repository(REPOSITORY, gh)
    return ci.Dispatcher(
        repository, paths.world_dir(), every, now=lambda: NOON, sleep=lambda _: None
    )


def _play(loaded: pd.DataFrame, step: simulate.Step, end: date) -> None:
    world = simulate.World(loaded, paths.data_dir(), paths.world_dir(), steps=(step,))
    simulate.simulate(world, FIXTURE_START, end, FAST)


def _dispatches() -> list[dict[str, object]]:
    path = paths.world_dir() / "ci_dispatches.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_scheduled_workflow_is_dispatched_every_n_simulated_days(
    loaded: pd.DataFrame, gh: FakeGh
) -> None:
    _play(loaded, _dispatcher(gh, every=3), end=date(2016, 1, 7))

    assert gh.named("api", "-X", "POST") == [DISPATCH] * 3
    assert _dispatches() == [
        {"business_date": day, "dispatched_at": "2026-10-29T12:00:00+00:00", "run_id": run}
        for day, run in [("2016-01-01", 1000), ("2016-01-04", 1001), ("2016-01-07", 1002)]
    ]


def test_dispatches_are_kept_out_of_the_answer_key_and_the_data_folder(
    loaded: pd.DataFrame, gh: FakeGh
) -> None:
    _play(loaded, _dispatcher(gh, every=1), end=date(2016, 1, 3))

    assert not (paths.world_dir() / "answer_key.jsonl").exists()
    data_files = [p.name for p in paths.data_dir().rglob("*") if p.is_file()]
    assert all(name.endswith(".parquet") for name in data_files)


def test_the_run_id_is_the_run_the_dispatch_created(loaded: pd.DataFrame, gh: FakeGh) -> None:
    gh.runs = [7, 8]

    _play(loaded, _dispatcher(gh, every=7), end=FIXTURE_START)

    assert _dispatches()[0]["run_id"] == 1002


def test_a_run_id_that_cannot_be_found_is_recorded_as_null(
    loaded: pd.DataFrame, gh: FakeGh, caplog: pytest.LogCaptureFixture
) -> None:
    gh.runs_listed = False

    _play(loaded, _dispatcher(gh, every=2), end=date(2016, 1, 3))

    assert [d["run_id"] for d in _dispatches()] == [None, None]
    assert "no run found" in caplog.text
    state = json.loads((paths.world_dir() / "state.json").read_text(encoding="utf-8"))
    assert state["last_done"] == "2016-01-03"


def test_a_failing_dispatch_is_logged_and_the_loop_goes_on(
    loaded: pd.DataFrame, gh: FakeGh, caplog: pytest.LogCaptureFixture
) -> None:
    gh.failing.append(("api", "-X", "POST"))

    _play(loaded, _dispatcher(gh, every=1), end=date(2016, 1, 3))

    assert len(gh.named("api", "-X", "POST")) == 3
    assert not (paths.world_dir() / "ci_dispatches.jsonl").exists()
    assert "HTTP 502" in caplog.text
    assert "dispatches" in caplog.text


def test_a_replayed_day_is_not_dispatched_twice(gh: FakeGh) -> None:
    dispatcher = _dispatcher(gh, every=1)

    dispatcher.end_of_day(FIXTURE_START, FIXTURE_START)
    dispatcher.end_of_day(FIXTURE_START, FIXTURE_START)

    assert len(gh.named("api", "-X", "POST")) == 1
    assert len(_dispatches()) == 1


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_ci_dispatch_needs_a_repository(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    assert cli.main(["simulate", *FIVE_DAYS, "--ci-every", "2"]) == 1

    assert "HOTEL_DEMO_REPO" in capsys.readouterr().err
    assert not paths.data_dir().exists()


@pytest.mark.parametrize(("option", "days"), [([], [1]), (["--ci-every", "2"], [1, 3, 5])])
def test_the_cli_dispatches_ci_for_the_runs_repository(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch, option: list[str], days: list[int]
) -> None:
    _place_source()
    monkeypatch.setattr(github, "RUNNER", gh)
    monkeypatch.setenv("HOTEL_DEMO_REPO", REPOSITORY)

    assert cli.main(["simulate", *FIVE_DAYS, *option]) == 0

    assert [d["business_date"] for d in _dispatches()] == [f"2016-01-0{n}" for n in days]


def test_ci_every_must_be_at_least_one() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["simulate", *FIVE_DAYS, "--ci-every", "0"])

    assert raised.value.code == 2


def test_the_dispatch_record_holds_the_real_time(gh: FakeGh, tmp_path: Path) -> None:
    times = iter([datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)])
    dispatcher = ci.Dispatcher(
        github.Repository(REPOSITORY, gh),
        tmp_path,
        1,
        now=lambda: next(times),
        sleep=lambda _: None,
    )

    dispatcher.end_of_day(FIXTURE_START, FIXTURE_START)

    line = (tmp_path / "ci_dispatches.jsonl").read_text(encoding="utf-8")
    assert json.loads(line)["dispatched_at"] == "2026-01-02T03:04:05+00:00"
