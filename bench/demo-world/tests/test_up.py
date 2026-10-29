"""`up`: the company's MLflow server and the day loop together, the server played by the fake.

The fake server runs a child of its own, as `uv run mlflow server` does; every test checks that
neither process outlives `up`, on Windows too.
"""

import os
import shutil
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from demo_world import cli, company, fetch, newrun, paths, simulate, source, up
from tests.company_fixture import FAKE_CLI, calls, fake_company, make_checkout
from tests.fixture import FIXTURE_CSV, FIXTURE_START

FAST = simulate.Pace(seconds_per_day=0)


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return make_checkout(tmp_path)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION; a process that is gone cannot be opened.
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    # A zombie has exited; only its parent has not collected it yet.
    return not (stat.exists() and stat.read_text().split(")")[-1].split()[0] == "Z")


def _server_pids(checkout: Path) -> list[int]:
    """The fake server, its child and, once it has started, the interpreter that child runs."""
    files = [checkout / "server_pids.txt", checkout / "sleeper_pid.txt"]
    return [int(pid) for file in files if file.exists() for pid in file.read_text().split()]


def _assert_all_stopped(pids: list[int]) -> None:
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert [pid for pid in pids if _alive(pid)] == []


def _up_with[T](checkout: Path, play: Callable[[], T], timeout: float = 30) -> T:
    def started() -> T:
        # Play only once the server's child runs, so every test stops a whole tree.
        deadline = time.monotonic() + 10
        while not (checkout / "sleeper_pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(_server_pids(checkout)) == 3
        return play()

    return up.up(
        fake_company(checkout),
        paths.world_dir(),
        newrun.free_port(),
        started,
        health_timeout=timeout,
    )


def test_up_serves_mlflow_while_the_loop_plays_then_stops_both(
    loaded: pd.DataFrame, checkout: Path
) -> None:
    the_company = fake_company(checkout)
    world = simulate.World(loaded, paths.data_dir(), paths.world_dir(), company=the_company)
    seen: list[str] = []

    def play() -> simulate.Summary:
        uri = os.environ["MLFLOW_TRACKING_URI"]
        seen.append(uri)
        with urllib.request.urlopen(f"{uri}/health", timeout=5) as response:  # noqa: S310
            assert response.read() == b"OK"
        # The server's processes run while the loop plays, so "stopped" below means it.
        assert [_alive(pid) for pid in _server_pids(checkout)] == [True, True, True]
        return simulate.simulate(world, FIXTURE_START, date(2016, 1, 2), FAST)

    summary = _up_with(checkout, play)

    assert summary.days == 2
    _assert_all_stopped(_server_pids(checkout))
    daily = [c for c in calls(checkout) if c["argv"][:2] == ["hotel-demo", "daily"]]
    assert {c["MLFLOW_TRACKING_URI"] for c in daily} == set(seen)
    assert "MLFLOW_TRACKING_URI" not in os.environ


def test_ctrl_c_during_the_loop_stops_both_processes(loaded: pd.DataFrame, checkout: Path) -> None:
    def interrupted(_: float) -> None:
        raise KeyboardInterrupt

    pace = simulate.Pace(seconds_per_day=60, sleep=interrupted)
    world = simulate.World(
        loaded, paths.data_dir(), paths.world_dir(), company=fake_company(checkout)
    )

    with pytest.raises(KeyboardInterrupt):
        _up_with(checkout, lambda: simulate.simulate(world, FIXTURE_START, date(2016, 1, 3), pace))

    _assert_all_stopped(_server_pids(checkout))
    state = (paths.world_dir() / "state.json").read_text(encoding="utf-8")
    assert '"last_done": "2016-01-01"' in state


def test_an_error_in_the_loop_stops_both_processes(checkout: Path) -> None:
    def broken() -> None:
        raise RuntimeError("the loop broke")

    with pytest.raises(RuntimeError, match="the loop broke"):
        _up_with(checkout, broken)

    _assert_all_stopped(_server_pids(checkout))


def test_a_child_that_ignores_the_stop_signal_is_stopped_too(checkout: Path) -> None:
    # The server exits on the signal and its child does not: the world must still reach it.
    (checkout / "server_stubborn").touch()

    _up_with(checkout, lambda: None)

    _assert_all_stopped(_server_pids(checkout))


def test_a_server_that_never_answers_is_stopped_with_a_clear_error(checkout: Path) -> None:
    (checkout / "server_silent").touch()
    played: list[bool] = []

    with pytest.raises(up.UpError, match="did not answer"):
        _up_with(checkout, lambda: played.append(True), timeout=1)

    assert played == []
    _assert_all_stopped(_server_pids(checkout))


def test_a_server_that_exits_is_reported_with_its_exit_code(checkout: Path) -> None:
    (checkout / "failures.json").write_text('["mlflow server"]', encoding="utf-8")

    with pytest.raises(up.UpError, match="exit code 3"):
        _up_with(checkout, lambda: None)

    assert "failed on purpose" in (paths.world_dir() / up.SERVER_LOG).read_text(encoding="utf-8")


def test_the_companys_environment_is_ready_before_the_server_starts(checkout: Path) -> None:
    _up_with(checkout, lambda: None)

    assert [c["argv"][:2] for c in calls(checkout)] == [
        ["mlflow", "--version"],
        ["mlflow", "server"],
    ]


def test_a_company_environment_without_mlflow_is_reported_before_any_server(
    checkout: Path,
) -> None:
    (checkout / "failures.json").write_text('["mlflow --version"]', encoding="utf-8")

    with pytest.raises(up.UpError, match="does not run from the company's environment"):
        _up_with(checkout, lambda: None)

    assert not (checkout / "server_pids.txt").exists()


def test_the_server_serves_the_companys_store_and_keeps_artifacts_in_its_folder(
    checkout: Path,
) -> None:
    the_company = fake_company(checkout)

    _up_with(checkout, lambda: None)

    (server,) = [c["argv"] for c in calls(checkout) if c["argv"][:2] == ["mlflow", "server"]]
    store = (paths.mlflow_dir() / "mlflow.db").resolve().as_posix()
    assert server[server.index("--backend-store-uri") + 1] == f"sqlite:///{store}"
    artifacts = (paths.mlflow_dir() / "artifacts").resolve().as_uri()
    assert server[server.index("--artifacts-destination") + 1] == artifacts
    assert server[server.index("--host") + 1] == "127.0.0.1"
    assert the_company.store() == paths.mlflow_dir().resolve() / "mlflow.db"


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_the_cli_runs_the_server_and_the_loop_on_the_runs_port(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_source()
    port = newrun.free_port()
    monkeypatch.setenv("HOTEL_DEMO_COMPANY", str(checkout))
    monkeypatch.setenv("HOTEL_DEMO_MLFLOW_PORT", str(port))
    monkeypatch.setattr(company, "uv_launcher", lambda _: (sys.executable, str(FAKE_CLI)))

    argv = ["up", "--start", "2016-01-01", "--end", "2016-01-02", "--seconds-per-day", "0"]
    assert cli.main(argv) == 0

    _assert_all_stopped(_server_pids(checkout))
    daily = [c for c in calls(checkout) if c["argv"][:2] == ["hotel-demo", "daily"]]
    assert {c["MLFLOW_TRACKING_URI"] for c in daily} == {f"http://127.0.0.1:{port}"}
    assert "2016-01-01 to 2016-01-02 (2 days)" in capsys.readouterr().out


def test_the_cli_needs_the_company_for_up(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    argv = ["up", "--start", "2016-01-01", "--end", "2016-01-02", "--seconds-per-day", "0"]
    assert cli.main(argv) == 1

    assert paths.COMPANY_VARIABLE in capsys.readouterr().err


def test_ctrl_c_ends_the_cli_with_130(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _place_source()
    monkeypatch.setenv("HOTEL_DEMO_COMPANY", str(checkout))
    monkeypatch.setenv("HOTEL_DEMO_MLFLOW_PORT", str(newrun.free_port()))
    monkeypatch.setattr(company, "uv_launcher", lambda _: (sys.executable, str(FAKE_CLI)))

    def interrupted(*_: object) -> simulate.Summary:
        raise KeyboardInterrupt

    monkeypatch.setattr(simulate, "simulate", interrupted)

    argv = ["up", "--start", "2016-01-01", "--end", "2016-01-02", "--seconds-per-day", "0"]
    assert cli.main(argv) == 130

    _assert_all_stopped(_server_pids(checkout))
    assert "stopped" in capsys.readouterr().err
