import json
import shutil
from pathlib import Path

import pytest

from demo_world import cli, fetch, paths
from tests.fixture import DEFAULT_SCENARIO, FIXTURE_CSV, Served

FIVE_DAYS = ["--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "0"]


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_fetch_downloads_the_pinned_source(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch, "SOURCE_URL", served.url)
    monkeypatch.setattr(fetch, "SOURCE_SHA256", served.sha256)

    assert cli.main(["fetch"]) == 0

    assert (paths.source_dir() / "hotels.csv").read_bytes() == FIXTURE_CSV.read_bytes()
    assert str(paths.source_dir() / "hotels.csv") in capsys.readouterr().out


def test_fetch_reports_a_wrong_hash(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch, "SOURCE_URL", served.url)
    monkeypatch.setattr(fetch, "SOURCE_SHA256", "0" * 64)

    assert cli.main(["fetch"]) == 1

    assert served.sha256 in capsys.readouterr().err


def test_backfill_writes_the_history(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    assert cli.main(["backfill", "--until", "2016-01-01"]) == 0

    assert len(list((paths.data_dir() / "bookings").iterdir())) == 10
    assert len(list((paths.data_dir() / "labels").iterdir())) == 8
    assert "10 bookings files" in capsys.readouterr().out


def test_backfill_without_the_source_says_to_fetch_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "--until", "2016-01-01"]) == 1

    assert "demo-world fetch" in capsys.readouterr().err
    assert not paths.data_dir().exists()


def test_backfill_refuses_a_data_folder_with_files(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()
    paths.data_dir().mkdir(parents=True)
    (paths.data_dir() / "notes.txt").write_text("x", encoding="utf-8")

    assert cli.main(["backfill", "--until", "2016-01-01"]) == 1

    assert "already has files" in capsys.readouterr().err


def test_until_must_be_a_date() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["backfill", "--until", "January"])

    assert raised.value.code == 2


def test_simulate_plays_the_days_after_the_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_source()
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(
        '[[injection]]\nkind = "ci_failure"\nstart = 2016-01-02\nend = 2016-01-03\n',
        encoding="utf-8",
    )
    assert cli.main(["backfill", "--until", "2016-01-01"]) == 0

    assert cli.main(["simulate", *FIVE_DAYS, "--scenario", str(scenario)]) == 0

    assert len(list((paths.data_dir() / "bookings").iterdir())) == 15
    key = (paths.world_dir() / "answer_key.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in key] == ["ci_failure"]
    assert "2016-01-01 to 2016-01-05 (5 days)" in capsys.readouterr().out


def test_simulate_without_a_scenario_injects_and_lists_nothing() -> None:
    _place_source()

    assert cli.main(["simulate", *FIVE_DAYS]) == 0

    assert (paths.world_dir() / "planted_flaws.json").exists()
    assert not (paths.world_dir() / "answer_key.jsonl").exists()


def test_simulate_with_the_default_scenario_runs() -> None:
    _place_source()

    assert cli.main(["simulate", *FIVE_DAYS, "--scenario", str(DEFAULT_SCENARIO)]) == 0


def test_simulate_says_when_nothing_is_left(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()
    assert cli.main(["simulate", *FIVE_DAYS]) == 0

    assert cli.main(["simulate", *FIVE_DAYS]) == 0

    assert "nothing left to play up to 2016-01-05" in capsys.readouterr().out


def test_simulate_without_the_source_says_to_fetch_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["simulate", *FIVE_DAYS]) == 1

    assert "demo-world fetch" in capsys.readouterr().err
    assert not paths.data_dir().exists()


def test_simulate_reports_a_bad_scenario_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_source()
    scenario = tmp_path / "bad.toml"
    scenario.write_text('[[injection]]\nkind = "flood"\n', encoding="utf-8")

    assert cli.main(["simulate", *FIVE_DAYS, "--scenario", str(scenario)]) == 1

    assert "bad.toml" in capsys.readouterr().err
    assert not paths.data_dir().exists()


def test_simulate_reports_an_end_before_the_start(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    argv = ["simulate", "--start", "2016-01-05", "--end", "2016-01-01", "--seconds-per-day", "0"]
    assert cli.main(argv) == 1

    assert "before the start" in capsys.readouterr().err


def test_seconds_per_day_must_not_be_negative() -> None:
    argv = ["simulate", "--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "-1"]
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2
