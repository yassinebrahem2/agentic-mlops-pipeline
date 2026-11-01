import logging
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import pytest

from demo_world import company, paths
from tests.company_fixture import (
    DAILY,
    Recorder,
    calls,
    commands,
    fail,
    fake_company,
    job,
    make_checkout,
)

DAY = date(2016, 1, 1)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return make_checkout(tmp_path)


def test_the_cli_runs_in_the_checkout_with_the_worlds_folders(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "the world's own environment")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5123")

    result = fake_company(checkout).cli(["daily", "--date", "2016-01-01"])

    assert result.code == 0
    (call,) = calls(checkout)
    assert call["argv"] == ["hotel-demo", "daily", "--date", "2016-01-01"]
    assert call["HOTEL_DEMO_DATA"] == str(paths.data_dir())
    assert call["HOTEL_DEMO_MLFLOW"] == str(paths.mlflow_dir())
    assert call["MLFLOW_TRACKING_URI"] == "http://127.0.0.1:5123"
    assert call["VIRTUAL_ENV"] is None


def test_the_company_is_run_with_uv_from_its_checkout(checkout: Path) -> None:
    argvs: list[list[str]] = []
    cwds: list[Path] = []

    def runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> company.Finished:
        # Records only: nothing is started.
        argvs.append(list(argv))
        cwds.append(cwd)
        return company.Finished(0, "", "")

    real = company.Company.at(checkout, paths.data_dir(), paths.mlflow_dir(), runner=runner)

    real.update()
    real.cli(["daily", "--date", "2016-01-01"])

    assert argvs[0] == ["git", "pull", "--ff-only"]
    # --locked: a stale uv.lock fails the command instead of being rewritten in the checkout,
    # which would block every later `git pull --ff-only`.
    assert argvs[1] == [
        "uv",
        "run",
        "--locked",
        "--project",
        str(checkout),
        "hotel-demo",
        "daily",
        "--date",
        "2016-01-01",
    ]
    assert cwds == [checkout, checkout]


def test_the_production_probe_runs_mlflow_from_the_locked_environment(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argvs: list[list[str]] = []

    def runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> company.Finished:
        argvs.append(list(argv))
        return company.Finished(0, "", "")

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5123")
    real = company.Company.at(checkout, paths.data_dir(), paths.mlflow_dir(), runner=runner)

    assert real.production_model() is company.Production.PRESENT
    assert argvs[0][:6] == ["uv", "run", "--locked", "--project", str(checkout), "mlflow"]


def test_relative_folders_are_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The company runs in its checkout: a path relative to the world's folder would miss.
    monkeypatch.chdir(tmp_path)

    real = company.Company.at(Path("company"), Path("data"), Path("mlflow"))

    assert real.checkout == tmp_path.resolve() / "company"
    assert real.data == tmp_path.resolve() / "data"
    assert real.mlflow == tmp_path.resolve() / "mlflow"
    assert real.launcher[-1] == str(tmp_path.resolve() / "company")


def test_a_failing_command_is_logged_with_its_exit_code_and_output(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fail(checkout, "hotel-demo daily")

    result = fake_company(checkout).cli(["daily", "--date", "2016-01-01"])

    assert result.code == 3
    assert "hotel-demo daily --date 2016-01-01" in caplog.text
    assert "exit code 3" in caplog.text
    assert "hotel-demo failed on purpose" in caplog.text


def test_a_program_that_cannot_start_is_a_logged_failure(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = company.Company(
        checkout=checkout,
        data=paths.data_dir(),
        mlflow=paths.mlflow_dir(),
        launcher=("demo-world-no-such-program",),
    )

    result = missing.cli(["daily", "--date", "2016-01-01"])

    assert result.code != 0
    assert "demo-world-no-such-program" in caplog.text


def test_update_pulls_the_checkout_fast_forward_only(checkout: Path) -> None:
    recorder = Recorder()

    fake_company(checkout, recorder).update()

    assert recorder.argvs == [["git", "pull", "--ff-only"]]
    assert recorder.cwds == [checkout]


def test_a_failing_pull_is_logged_and_not_raised(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = Recorder(
        on_git=lambda: company.Finished(128, "", "fatal: Not possible to fast-forward")
    )

    result = fake_company(checkout, recorder).update()

    assert result.code == 128
    assert "git pull --ff-only" in caplog.text
    assert "Not possible to fast-forward" in caplog.text


def test_no_store_means_no_production_model_without_asking(checkout: Path) -> None:
    assert fake_company(checkout).production_model() is company.Production.ABSENT
    assert calls(checkout) == []


def test_the_probe_reads_the_documented_store(checkout: Path) -> None:
    fake = fake_company(checkout)
    paths.mlflow_dir().mkdir()
    (paths.mlflow_dir() / "mlflow.db").touch()

    assert fake.production_model() is company.Production.ABSENT
    fake.cli(["deploy", "--version", "1"])
    assert fake.production_model() is company.Production.PRESENT

    probes = [c for c in calls(checkout) if c["argv"][0] == "mlflow"]
    store = (paths.mlflow_dir() / "mlflow.db").resolve().as_posix()
    assert [c["MLFLOW_TRACKING_URI"] for c in probes] == [f"sqlite:///{store}"] * 2
    assert probes[0]["argv"][:4] == [
        "mlflow",
        "artifacts",
        "download",
        "--artifact-uri",
    ]
    assert probes[0]["argv"][4] == "models:/hotel-cancellation@production"


def test_the_probe_asks_the_tracking_server_when_one_is_set(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5123")

    assert fake_company(checkout).production_model() is company.Production.ABSENT

    (probe,) = calls(checkout)
    assert probe["MLFLOW_TRACKING_URI"] == "http://127.0.0.1:5123"


def test_a_probe_that_breaks_otherwise_is_unknown_and_logged(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    paths.mlflow_dir().mkdir()
    (paths.mlflow_dir() / "mlflow.db").touch()
    fail(checkout, "mlflow")

    assert fake_company(checkout).production_model() is company.Production.UNKNOWN
    assert "mlflow failed on purpose" in caplog.text


def test_bootstrap_trains_evaluates_and_deploys_the_first_model(checkout: Path) -> None:
    fake_company(checkout).bootstrap(cutoff=DAY)

    assert commands(checkout) == [
        "hotel-demo train --cutoff 2016-01-01",
        "hotel-demo evaluate --version 1",
        "hotel-demo deploy --version 1",
    ]


def test_bootstrap_leaves_a_production_model_alone(checkout: Path) -> None:
    fake = fake_company(checkout)
    fake.bootstrap(cutoff=DAY)

    fake.bootstrap(cutoff=date(2016, 1, 9))

    assert commands(checkout)[3:] == ["mlflow artifacts download"]


def test_bootstrap_does_nothing_when_the_probe_cannot_tell(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    paths.mlflow_dir().mkdir()
    (paths.mlflow_dir() / "mlflow.db").touch()
    fail(checkout, "mlflow")

    fake_company(checkout).bootstrap(cutoff=DAY)

    assert commands(checkout) == ["mlflow artifacts download"]
    assert "cannot tell whether" in caplog.text


@pytest.mark.parametrize(
    ("failing", "ran"),
    [
        ("hotel-demo train", ["train"]),
        ("hotel-demo evaluate", ["train", "evaluate"]),
    ],
)
def test_bootstrap_stops_at_the_first_failing_step(
    checkout: Path, failing: str, ran: list[str]
) -> None:
    fail(checkout, failing)

    fake_company(checkout).bootstrap(cutoff=DAY)

    assert [c["argv"][1] for c in calls(checkout)] == ran


def test_bootstrap_needs_the_version_train_prints_last(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> company.Finished:
        return company.Finished(0, "trained\n\n", "")

    fake_company(checkout, runner).bootstrap(cutoff=DAY)

    assert "did not print a model version" in caplog.text


def test_the_schedule_lists_the_jobs_in_file_order(checkout: Path) -> None:
    (checkout / "ops" / "schedule.toml").write_text(
        DAILY + job("retrain", "hotel-demo train --cutoff {date}", 7), encoding="utf-8"
    )

    assert company.read_schedule(checkout) == (
        company.Job("daily", "hotel-demo daily --date {date}", 1),
        company.Job("retrain", "hotel-demo train --cutoff {date}", 7),
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "cannot read"),
        ("[[jobs]\n", "not valid TOML"),
        ("[[jobs]]\nname = 'x'\ncommand = 'hotel-demo daily'\n", "every"),
        ("[[jobs]]\nname = 'x'\ncommand = 'hotel-demo daily'\nevery = 0\n", "every"),
        ("[[jobs]]\nname = 'x'\ncommand = 3\nevery = 1\n", "command"),
    ],
)
def test_a_bad_schedule_is_refused_with_a_reason(
    checkout: Path, text: str | None, message: str
) -> None:
    schedule = checkout / "ops" / "schedule.toml"
    if text is None:
        schedule.unlink()
    else:
        schedule.write_text(text, encoding="utf-8")

    with pytest.raises(company.CompanyError, match=message):
        company.read_schedule(checkout)


def test_a_job_is_due_every_n_days_from_the_origin() -> None:
    weekly = company.Job("weekly", "hotel-demo daily --date {date}", 7)
    due = [n for n in range(15) if weekly.due(date(2016, 1, 1 + n), origin=DAY)]
    assert due == [0, 7, 14]


def test_the_due_jobs_run_in_file_order_with_the_date_filled_in(checkout: Path) -> None:
    (checkout / "ops" / "schedule.toml").write_text(
        DAILY + job("every-other", "hotel-demo score --date {date}", 2), encoding="utf-8"
    )
    fake = fake_company(checkout)

    for day in (date(2016, 1, 1), date(2016, 1, 2), date(2016, 1, 3)):
        fake.run_jobs(day, origin=DAY)

    assert commands(checkout) == [
        "hotel-demo daily --date 2016-01-01",
        "hotel-demo score --date 2016-01-01",
        "hotel-demo daily --date 2016-01-02",
        "hotel-demo daily --date 2016-01-03",
        "hotel-demo score --date 2016-01-03",
    ]


def test_a_job_that_is_not_a_company_command_is_skipped_and_logged(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (checkout / "ops" / "schedule.toml").write_text(
        job("cleanup", "rm -rf {date}", 1) + DAILY, encoding="utf-8"
    )

    fake_company(checkout).run_jobs(DAY, origin=DAY)

    assert commands(checkout) == ["hotel-demo daily --date 2016-01-01"]
    assert "cleanup" in caplog.text
    assert "hotel-demo" in caplog.text


def test_a_bad_schedule_skips_the_days_jobs_and_is_logged(
    checkout: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (checkout / "ops" / "schedule.toml").write_text("[[jobs]\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        fake_company(checkout).run_jobs(DAY, origin=DAY)

    assert calls(checkout) == []
    assert "not valid TOML" in caplog.text
