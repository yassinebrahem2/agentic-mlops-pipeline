"""`new-run`, `--run` and the original repository's refusal, against a fake `gh`."""

import shutil
import socket
import sys
from pathlib import Path

import pytest

from demo_world import cli, company, fetch, github, newrun, paths
from tests.company_fixture import FAKE_CLI, commands
from tests.fake_gh import FakeGh
from tests.fixture import FIXTURE_CSV

FIVE_DAYS = ["--start", "2016-01-01", "--end", "2016-01-05", "--seconds-per-day", "0"]
NO_WAIT = {"sleep": lambda _: None}


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(github, "RUNNER", fake)
    return fake


def _settings(run: str) -> dict[str, str]:
    text = (paths.runs_dir() / run / "run.env").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines())


def test_new_run_copies_the_template_and_writes_every_setting(gh: FakeGh) -> None:
    folder = newrun.new_run("rehearsal", gh, **NO_WAIT)

    assert folder == paths.runs_dir() / "rehearsal"
    assert gh.named("api", "user") == [["api", "user", "--jq", ".login"]]
    (generate,) = gh.named("api", "-X", "POST")
    assert generate == [
        "api",
        "-X",
        "POST",
        "repos/yassinebrahem2/hotel-cancellation-demo/generate",
        "-f",
        "owner=tester",
        "-f",
        "name=hotel-cancellation-demo-rehearsal",
        "-F",
        "private=false",
    ]
    assert gh.named("repo", "clone") == [
        ["repo", "clone", "tester/hotel-cancellation-demo-rehearsal", str(folder / "company")]
    ]
    settings = _settings("rehearsal")
    assert list(settings) == list(paths.RUN_SETTINGS)
    assert settings["HOTEL_DEMO_REPO"] == "tester/hotel-cancellation-demo-rehearsal"
    for variable, name in [
        ("HOTEL_DEMO_COMPANY", "company"),
        ("HOTEL_DEMO_DATA", "data"),
        ("HOTEL_DEMO_MLFLOW", "mlflow"),
        ("HOTEL_DEMO_WORLD", "world"),
    ]:
        assert settings[variable] == str(folder / name)
        assert (folder / name).is_dir()
    assert [list((folder / name).iterdir()) for name in ("data", "mlflow", "world")] == [[]] * 3
    port = int(settings["HOTEL_DEMO_MLFLOW_PORT"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_new_run_waits_for_the_copy_to_have_its_content(gh: FakeGh) -> None:
    gh.empty_checks = 2

    newrun.new_run("slow", gh, **NO_WAIT)

    assert len(gh.named("api", "repos/tester/hotel-cancellation-demo-slow/commits?per_page=1")) == 3
    assert len(gh.named("repo", "clone")) == 1


def test_new_run_gives_up_on_a_copy_that_stays_empty(gh: FakeGh) -> None:
    gh.empty_checks = 1000

    with pytest.raises(newrun.RunError, match="gh repo delete tester/hotel-cancellation-demo-x"):
        newrun.new_run("x", gh, **NO_WAIT)

    assert gh.named("repo", "clone") == []


def test_new_run_refuses_an_existing_run_before_calling_github(gh: FakeGh) -> None:
    (paths.runs_dir() / "taken").mkdir(parents=True)

    with pytest.raises(newrun.RunError, match="already exists"):
        newrun.new_run("taken", gh, **NO_WAIT)

    assert gh.calls == []


@pytest.mark.parametrize("name", ["", "Upper", "a b", "../escape", "-dash", "x" * 41])
def test_new_run_refuses_a_name_unfit_for_a_repository(gh: FakeGh, name: str) -> None:
    with pytest.raises(newrun.RunError, match="run name"):
        newrun.new_run(name, gh, **NO_WAIT)

    assert gh.calls == []


def test_a_failing_github_call_stops_new_run_with_its_output(gh: FakeGh) -> None:
    gh.failing.append(("api", "-X", "POST"))

    with pytest.raises(github.GitHubError, match="HTTP 502"):
        newrun.new_run("down", gh, **NO_WAIT)

    assert not (paths.runs_dir() / "down").exists()


def test_two_runs_get_disjoint_folders_and_ports(gh: FakeGh) -> None:
    first = newrun.new_run("one", gh, **NO_WAIT)
    second = newrun.new_run("two", gh, **NO_WAIT)

    one, two = _settings("one"), _settings("two")
    for variable in paths.RUN_SETTINGS:
        assert one[variable] != two[variable]
    assert not first.is_relative_to(second)
    assert not second.is_relative_to(first)


def test_a_port_taken_by_another_run_is_not_picked_again(gh: FakeGh) -> None:
    ports = iter([5001, 5001, 5002])

    newrun.new_run("one", gh, free_port=lambda: next(ports), **NO_WAIT)
    newrun.new_run("two", gh, free_port=lambda: next(ports), **NO_WAIT)

    assert _settings("two")["HOTEL_DEMO_MLFLOW_PORT"] == "5002"


def test_loading_a_run_that_does_not_exist_says_how_to_create_it() -> None:
    with pytest.raises(newrun.RunError, match="demo-world new-run ghost"):
        newrun.load_run("ghost")


def test_loading_a_run_refuses_a_setting_it_does_not_know(gh: FakeGh) -> None:
    folder = newrun.new_run("odd", gh, **NO_WAIT)
    with (folder / "run.env").open("a", encoding="utf-8") as file:
        file.write("PATH=/elsewhere\n")

    with pytest.raises(newrun.RunError, match="PATH"):
        newrun.load_run("odd")


def test_the_cli_creates_a_run(gh: FakeGh, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["new-run", "demo"]) == 0

    assert gh.created == ["tester/hotel-cancellation-demo-demo"]
    out = capsys.readouterr().out
    assert "--run demo" in out
    assert "tester/hotel-cancellation-demo-demo" in out


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_every_command_runs_in_the_folders_of_its_run(gh: FakeGh, isolated_folders: Path) -> None:
    _place_source()
    assert cli.main(["new-run", "demo"]) == 0
    folder = paths.runs_dir() / "demo"

    assert cli.main(["backfill", "--run", "demo", "--until", "2016-01-01"]) == 0

    assert any((folder / "data" / "bookings").iterdir())
    assert not (isolated_folders / "data").exists()


def test_two_runs_each_bootstrap_their_own_model(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _place_source()
    monkeypatch.setattr(company, "uv_launcher", lambda _: (sys.executable, str(FAKE_CLI)))
    for run in ("one", "two"):
        assert cli.main(["new-run", run]) == 0

    for run in ("one", "two"):
        assert cli.main(["simulate", "--run", run, *FIVE_DAYS]) == 0

    for run in ("one", "two"):
        folder = paths.runs_dir() / run
        played = commands(folder / "company")
        assert played[:3] == [
            "hotel-demo train --cutoff 2016-01-01",
            "hotel-demo evaluate --version 1",
            "hotel-demo deploy --version 1",
        ]
        assert (folder / "mlflow" / "production").read_text(encoding="utf-8") == "1"
        assert (folder / "world" / "state.json").exists()


def test_a_run_without_its_settings_file_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["simulate", "--run", "ghost", *FIVE_DAYS]) == 1

    assert "demo-world new-run ghost" in capsys.readouterr().err


@pytest.mark.parametrize(
    "repository",
    ["yassinebrahem2/hotel-cancellation-demo", "YassineBrahem2/Hotel-Cancellation-Demo"],
)
@pytest.mark.parametrize(
    "command", [["simulate", *FIVE_DAYS], ["backfill", "--until", "2016-01-01"]]
)
def test_the_world_refuses_to_run_against_the_original_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repository: str,
    command: list[str],
) -> None:
    _place_source()
    monkeypatch.setenv("HOTEL_DEMO_REPO", repository)

    assert cli.main(command) == 1

    assert "original" in capsys.readouterr().err
    assert not paths.data_dir().exists()


def test_a_repository_setting_must_name_an_owner_and_a_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOTEL_DEMO_REPO", "just-a-name")

    assert cli.main(["simulate", *FIVE_DAYS]) == 1

    assert "owner/name" in capsys.readouterr().err


def test_without_a_repository_the_world_calls_no_github(gh: FakeGh) -> None:
    _place_source()

    assert cli.main(["simulate", *FIVE_DAYS]) == 0

    assert gh.calls == []


def test_the_run_settings_override_the_shell(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _place_source()
    assert cli.main(["new-run", "demo"]) == 0
    monkeypatch.setenv("HOTEL_DEMO_DATA", str(tmp_path / "shell-data"))

    assert cli.main(["backfill", "--run", "demo", "--until", "2016-01-01"]) == 0

    assert not (tmp_path / "shell-data").exists()
    assert (paths.runs_dir() / "demo" / "data" / "bookings").is_dir()
