"""The PR merger of evaluation runs: agents' PRs merged N simulated days after they opened."""

import shutil
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from demo_world import answer_key, cli, fetch, github, merger, paths, simulate, source
from demo_world.company import Finished
from tests.company_fixture import Recorder, fake_company, make_checkout
from tests.fake_gh import FakeGh, Pull, checks
from tests.fixture import FIXTURE_CSV, FIXTURE_START

REPOSITORY = "tester/hotel-cancellation-demo-merge"
FAST = simulate.Pace(seconds_per_day=0)
FIVE_DAYS = ["--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "0"]
BRANCH = "agent/data-quality/fix-nulls-a1b2c3"


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


@pytest.fixture
def gh() -> FakeGh:
    return FakeGh()


def _merger(gh: FakeGh, after_days: int) -> merger.Merger:
    return merger.Merger(github.Repository(REPOSITORY, gh), paths.world_dir(), after_days)


def _day(n: int) -> date:
    return FIXTURE_START + timedelta(days=n - 1)


def _play(the_merger: merger.Merger, first: int, last: int) -> None:
    for n in range(first, last + 1):
        the_merger.start_of_day(_day(n), FIXTURE_START)


def _decisions() -> list[dict[str, object]]:
    return [line for line in answer_key.lines(paths.world_dir()) if line["kind"].startswith("pr_")]


def _merge_call(number: int) -> list[str]:
    return ["pr", "merge", str(number), "--repo", REPOSITORY, "--merge"]


def test_a_green_agent_pr_is_merged_n_simulated_days_after_it_opened(gh: FakeGh) -> None:
    gh.pulls = [Pull(5, BRANCH)]
    the_merger = _merger(gh, after_days=2)

    _play(the_merger, 1, 2)
    assert gh.merged == []
    _play(the_merger, 3, 3)

    assert gh.named("pr", "merge") == [_merge_call(5)]
    assert _decisions() == [
        {
            "kind": "pr_merged",
            "pr": 5,
            "branch": BRANCH,
            "opened": "2016-01-01",
            "date": "2016-01-03",
            "delay_days": 2,
            "merge_after_days": 2,
            "reason": None,
        }
    ]


def test_the_opening_is_the_first_day_the_world_sees_the_pr(gh: FakeGh) -> None:
    the_merger = _merger(gh, after_days=2)
    _play(the_merger, 1, 2)
    gh.pulls = [Pull(6, BRANCH)]

    _play(the_merger, 3, 4)
    assert gh.merged == []
    _play(the_merger, 5, 5)

    assert gh.merged == [6]
    assert _decisions()[0]["opened"] == "2016-01-03"


@pytest.mark.parametrize(
    "branch", ["feature/fix", "agent/only-an-agent", "agentx/a/b", "fix/agent/a/b", "agent//t"]
)
def test_a_branch_that_is_not_an_agent_branch_is_never_merged(gh: FakeGh, branch: str) -> None:
    gh.pulls = [Pull(7, branch)]

    _play(_merger(gh, after_days=0), 1, 5)

    assert gh.named("pr", "merge") == []
    assert _decisions() == []


def test_a_pr_from_a_fork_is_never_merged_whatever_its_branch(gh: FakeGh) -> None:
    # The copy is public: a fork can name its branch like an agent's. Its code would run as
    # the user once merged and pulled, so the world never merges it.
    gh.pulls = [
        Pull(20, BRANCH, cross_repository=True),
        Pull(21, "feature/fork", cross_repository=True),
    ]

    _play(_merger(gh, after_days=1), 1, 5)

    assert gh.named("pr", "merge") == []
    assert [(d["pr"], d["kind"], d["reason"], d["date"]) for d in _decisions()] == [
        (20, "pr_not_merged", "cross_repository", "2016-01-02")
    ]


def test_a_conflict_is_recorded_once_and_left_for_the_agents(gh: FakeGh) -> None:
    gh.pulls = [Pull(8, BRANCH, mergeable="CONFLICTING")]

    _play(_merger(gh, after_days=1), 1, 5)

    assert gh.named("pr", "merge") == []
    assert [(d["kind"], d["reason"], d["date"], d["delay_days"]) for d in _decisions()] == [
        ("pr_not_merged", "conflict", "2016-01-02", 1)
    ]


def test_red_checks_are_refused_until_they_turn_green(gh: FakeGh) -> None:
    pull = Pull(9, BRANCH, rollup=checks("SUCCESS", "FAILURE"))
    gh.pulls = [pull]
    the_merger = _merger(gh, after_days=1)

    _play(the_merger, 1, 3)
    pull.rollup = checks("SUCCESS", "SUCCESS")
    _play(the_merger, 4, 4)

    assert gh.merged == [9]
    assert [(d["kind"], d["reason"], d["date"]) for d in _decisions()] == [
        ("pr_not_merged", "checks_failed", "2016-01-02"),
        ("pr_merged", None, "2016-01-04"),
    ]


@pytest.mark.parametrize(
    ("mergeable", "rollup"),
    [
        ("MERGEABLE", checks("SUCCESS", "PENDING")),
        ("MERGEABLE", []),
        ("UNKNOWN", checks("SUCCESS")),
    ],
)
def test_a_pr_still_being_checked_waits_without_a_record(
    gh: FakeGh, mergeable: str, rollup: list[dict[str, str]]
) -> None:
    gh.pulls = [Pull(10, BRANCH, mergeable=mergeable, rollup=rollup)]

    _play(_merger(gh, after_days=0), 1, 3)

    assert gh.merged == []
    assert _decisions() == []


def test_skipped_and_neutral_checks_and_successful_statuses_count_as_green(gh: FakeGh) -> None:
    rollup = [*checks("SKIPPED", "NEUTRAL"), {"__typename": "StatusContext", "state": "SUCCESS"}]
    gh.pulls = [Pull(11, BRANCH, rollup=rollup)]

    _play(_merger(gh, after_days=0), 1, 1)

    assert gh.merged == [11]


def test_a_failed_merge_is_recorded_as_not_merged(
    gh: FakeGh, caplog: pytest.LogCaptureFixture
) -> None:
    gh.pulls = [Pull(12, BRANCH)]
    gh.failing.append(("pr", "merge"))

    _play(_merger(gh, after_days=0), 1, 2)

    assert [(d["kind"], d["reason"]) for d in _decisions()] == [("pr_not_merged", "merge_failed")]
    assert "HTTP 502" in caplog.text


def test_a_failing_pr_list_is_logged_and_the_day_goes_on(
    gh: FakeGh, caplog: pytest.LogCaptureFixture
) -> None:
    gh.pulls = [Pull(13, BRANCH)]
    gh.failing_once.append(("pr", "list"))
    the_merger = _merger(gh, after_days=0)

    _play(the_merger, 1, 1)
    assert gh.merged == []
    assert "HTTP 502" in caplog.text
    _play(the_merger, 2, 2)

    assert gh.merged == [13]


def test_a_merge_takes_effect_the_same_day(loaded: pd.DataFrame, tmp_path: Path) -> None:
    events: list[str] = []
    fake = FakeGh(pulls=[Pull(14, BRANCH)])

    def gh(args: Sequence[str]) -> Finished:
        events.append(" ".join(args[:2]))
        return fake(args)

    def pull() -> Finished:
        events.append("git pull")
        return Finished(0, "", "")

    the_company = fake_company(make_checkout(tmp_path), Recorder(on_git=pull))
    the_merger = merger.Merger(github.Repository(REPOSITORY, gh), paths.world_dir(), 1)
    world = simulate.World(
        loaded, paths.data_dir(), paths.world_dir(), company=the_company, steps=(the_merger,)
    )

    simulate.simulate(world, FIXTURE_START, date(2016, 1, 2), FAST)

    assert events == ["pr list", "git pull", "pr list", "pr merge", "git pull"]


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_merging_needs_a_repository(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    assert cli.main(["simulate", *FIVE_DAYS, "--merge-after-days", "3"]) == 1

    assert "HOTEL_DEMO_REPO" in capsys.readouterr().err
    assert not paths.data_dir().exists()


@pytest.mark.parametrize(("option", "merged"), [([], []), (["--merge-after-days", "2"], [15])])
def test_the_world_merges_only_with_the_option(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch, option: list[str], merged: list[int]
) -> None:
    _place_source()
    gh.pulls = [Pull(15, BRANCH)]
    monkeypatch.setattr(github, "RUNNER", gh)
    monkeypatch.setenv("HOTEL_DEMO_REPO", REPOSITORY)

    assert cli.main(["simulate", *FIVE_DAYS, *option]) == 0

    assert gh.merged == merged
    if not option:
        assert gh.named("pr") == []


def test_merge_after_days_must_not_be_negative() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["simulate", *FIVE_DAYS, "--merge-after-days", "-1"])

    assert raised.value.code == 2
