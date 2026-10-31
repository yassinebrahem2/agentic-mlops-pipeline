"""`up`: the company's MLflow server and the day loop, started and stopped together.

The server runs from the company's environment, as its ops host would, on the company's own
store, and the loop runs with `MLFLOW_TRACKING_URI` pointing at it. The server runs in a
process group of its own (`process_group`), so the terminal's Ctrl+C reaches the world only,
and the world stops the whole group on every exit: the end of the loop, Ctrl+C, SIGTERM or
SIGHUP (`kill`, a closed terminal), an error, or a server that never answered.
"""

import logging
import os
import signal
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from demo_world import paths
from demo_world.company import Company, tail
from demo_world.process_group import ProcessGroup

HOST = "127.0.0.1"
SERVER_LOG = "mlflow-server.log"
HEALTH_TIMEOUT_SECONDS = 120.0
_POLL_SECONDS = 0.5

type Clock = Callable[[], float]

log = logging.getLogger(__name__)


class UpError(Exception):
    """The MLflow server could not be brought up."""


class Terminated(BaseException):
    """The world received SIGTERM or SIGHUP; raised like Ctrl+C, so every `finally` runs."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(f"stopped by signal {signal_number}")
        self.signal_number = signal_number


# Python's defaults end the process at once, skipping `finally`, and the server, in a session
# of its own, never sees the terminal's SIGHUP.
_TERMINATION_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@contextmanager
def _terminations_raise() -> Iterator[None]:
    def handler(signal_number: int, frame: FrameType | None) -> None:
        raise Terminated(signal_number)

    previous = {number: signal.signal(number, handler) for number in _TERMINATION_SIGNALS}
    try:
        yield
    finally:
        for number, handler_before in previous.items():
            signal.signal(number, handler_before)


def tracking_uri(port: int) -> str:
    return f"http://{HOST}:{port}"


def server_command(the_company: Company, port: int) -> list[str]:
    """`mlflow server` from the company's environment, on its store, its artifacts kept in its
    MLflow folder (never in the checkout, MLflow's default)."""
    artifacts = (the_company.mlflow / "artifacts").resolve().as_uri()
    return [
        *the_company.launcher,
        "mlflow",
        "server",
        "--backend-store-uri",
        f"sqlite:///{the_company.store().as_posix()}",
        "--artifacts-destination",
        artifacts,
        "--host",
        HOST,
        "--port",
        str(port),
    ]


def _prepare(the_company: Company) -> None:
    # The first run in a fresh clone builds the company's environment (MLflow, LightGBM...),
    # which may take minutes: done here, before the server's health timeout starts.
    argv = [*the_company.launcher, "mlflow", "--version"]
    result = the_company.runner(argv, the_company.checkout, the_company.environment())
    if not result.ok:
        raise UpError(
            f"MLflow does not run from the company's environment (exit code {result.code}):\n"
            f"{tail(result.stderr)}\n{tail(result.stdout)}"
        )


def wait_until_answering(
    server: ProcessGroup,
    url: str,
    timeout: float,
    output: Path,
    clock: Clock = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once `url` answers 200; fail if the server exits or the timeout passes."""
    deadline = clock() + timeout
    while True:
        code = server.exit_code()
        if code is not None:
            raise UpError(
                f"the MLflow server stopped with exit code {code} before answering; see {output}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 (a local http URL)
                if response.status == 200:
                    return
                last = f"HTTP {response.status}"
        except OSError as error:
            last = str(error)
        if clock() >= deadline:
            raise UpError(
                f"the MLflow server did not answer {url} within {timeout:g} s ({last}); "
                f"see {output}"
            )
        sleep(_POLL_SECONDS)


def up[T](
    the_company: Company,
    world: Path,
    port: int,
    play: Callable[[], T],
    *,
    health_timeout: float = HEALTH_TIMEOUT_SECONDS,
) -> T:
    """Serve the company's MLflow on `port`, run `play` against it, then stop the server."""
    _prepare(the_company)
    world.mkdir(parents=True, exist_ok=True)
    the_company.mlflow.mkdir(parents=True, exist_ok=True)
    output = world / SERVER_LOG
    uri = tracking_uri(port)
    previous = os.environ.get(paths.TRACKING_URI_VARIABLE)
    with output.open("ab") as server_output, _terminations_raise():
        server = ProcessGroup(
            server_command(the_company, port),
            the_company.checkout,
            the_company.environment(),
            server_output,
        )
        try:
            wait_until_answering(server, f"{uri}/health", health_timeout, output)
            log.info("the company's MLflow server answers at %s (log: %s)", uri, output)
            # The company and the production-model probe inherit it: one tracking URI.
            os.environ[paths.TRACKING_URI_VARIABLE] = uri
            return play()
        finally:
            server.stop()
            if previous is None:
                os.environ.pop(paths.TRACKING_URI_VARIABLE, None)
            else:
                os.environ[paths.TRACKING_URI_VARIABLE] = previous
            log.info("the company's MLflow server is stopped")
