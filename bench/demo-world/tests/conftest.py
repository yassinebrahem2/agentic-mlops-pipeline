import hashlib
import os
import threading
from collections.abc import Iterator, Sequence
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

from demo_world import github
from demo_world.company import Finished
from tests.fixture import FIXTURE_CSV, Served


@pytest.fixture(autouse=True)
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any `gh` call a test did not fake fail the test, so no test can reach GitHub."""

    def refuse(args: Sequence[str]) -> Finished:
        pytest.fail(f"a test called the real gh: gh {' '.join(args)}")

    monkeypatch.setattr(github, "RUNNER", refuse)


@pytest.fixture(autouse=True)
def isolated_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every folder, and the home folder itself, at the test's temporary folder."""
    # `--run` and `up` write settings into the environment; none may reach the next test.
    with mock.patch.dict(os.environ):
        yield _isolate(tmp_path, monkeypatch)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOTEL_DEMO_SOURCE", str(tmp_path / "source"))
    monkeypatch.setenv("HOTEL_DEMO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HOTEL_DEMO_WORLD", str(tmp_path / "world"))
    monkeypatch.setenv("HOTEL_DEMO_MLFLOW", str(tmp_path / "mlflow"))
    # A developer's own company checkout, tracking server or repository never reaches a test;
    # the tests against the real company read HOTEL_DEMO_COMPANY at import, before this runs.
    for variable in (
        "HOTEL_DEMO_COMPANY",
        "MLFLOW_TRACKING_URI",
        "HOTEL_DEMO_REPO",
        "HOTEL_DEMO_MLFLOW_PORT",
    ):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


@pytest.fixture
def served() -> Iterator[Served]:
    """Serve the fixture CSV on localhost, recording every request."""
    # Hash the bytes on disk: a Windows checkout may have converted the CSV's line endings.
    sha256 = hashlib.sha256(FIXTURE_CSV.read_bytes()).hexdigest()
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(FIXTURE_CSV.parent))
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        yield Served(f"http://127.0.0.1:{port}/{FIXTURE_CSV.name}", sha256, requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
