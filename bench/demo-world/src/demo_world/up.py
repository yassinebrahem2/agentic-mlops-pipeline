"""`up`: the company's MLflow server and the day loop, started and stopped together.

The server runs from the company's environment, as its ops host would, on the company's own
store, and the loop runs with `MLFLOW_TRACKING_URI` pointing at it. The server is started in a
process group of its own, so the terminal's Ctrl+C reaches the world only, and the world stops
the whole group itself (`uv run` starts MLflow as a child, and MLflow its own workers) on every
exit: the end of the loop, Ctrl+C, an error, or a server that never answered.
"""

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

from demo_world import paths
from demo_world.company import Company

HOST = "127.0.0.1"
SERVER_LOG = "mlflow-server.log"
HEALTH_TIMEOUT_SECONDS = 120.0
STOP_GRACE_SECONDS = 15.0
_POLL_SECONDS = 0.5

type Clock = Callable[[], float]

log = logging.getLogger(__name__)


class UpError(Exception):
    """The MLflow server could not be brought up."""


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


def _start(argv: Sequence[str], the_company: Company, output: IO[bytes]) -> subprocess.Popen[bytes]:
    # Its output goes to a file: over a run of hours an unread pipe would fill and block it.
    if sys.platform == "win32":
        return subprocess.Popen(  # noqa: S603 (the world's own command, never a shell)
            list(argv),
            cwd=the_company.checkout,
            env=the_company.environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 (the world's own command, never a shell)
        list(argv),
        cwd=the_company.checkout,
        env=the_company.environment(),
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _interrupt(process: subprocess.Popen[bytes]) -> None:
    """Ask the server's whole process group to stop, as Ctrl+C in its own terminal would."""
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGTERM)


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill whatever is left of the server's process group."""
    if sys.platform == "win32":
        if process.poll() is None:
            # The tree, not just the process: `uv` would leave MLflow's processes running.
            done = subprocess.run(  # noqa: S603 (the system's own tool, never a shell)
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],  # noqa: S607 (a system tool)
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode != 0:
                log.error("taskkill of the MLflow server failed: %s", done.stderr.strip())
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # No process of the group is left: the interrupt stopped them all.
        return


def stop(process: subprocess.Popen[bytes], grace: float = STOP_GRACE_SECONDS) -> None:
    """Stop the server and every process it started, whatever state it is in."""
    if process.poll() is None:
        try:
            _interrupt(process)
            process.wait(grace)
        except OSError as error:
            log.warning("cannot interrupt the MLflow server (%s); killing it", error)
        except subprocess.TimeoutExpired:
            log.warning("the MLflow server did not stop within %g s; killing it", grace)
    _kill(process)
    process.wait()


def wait_until_answering(
    process: subprocess.Popen[bytes],
    url: str,
    timeout: float,
    output: Path,
    clock: Clock = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once `url` answers 200; fail if the server exits or the timeout passes."""
    deadline = clock() + timeout
    while True:
        code = process.poll()
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
    world.mkdir(parents=True, exist_ok=True)
    the_company.mlflow.mkdir(parents=True, exist_ok=True)
    output = world / SERVER_LOG
    uri = tracking_uri(port)
    previous = os.environ.get(paths.TRACKING_URI_VARIABLE)
    with output.open("ab") as server_output:
        process = _start(server_command(the_company, port), the_company, server_output)
        try:
            wait_until_answering(process, f"{uri}/health", health_timeout, output)
            log.info("the company's MLflow server answers at %s (log: %s)", uri, output)
            # The company and the production-model probe inherit it: one tracking URI.
            os.environ[paths.TRACKING_URI_VARIABLE] = uri
            return play()
        finally:
            stop(process)
            if previous is None:
                os.environ.pop(paths.TRACKING_URI_VARIABLE, None)
            else:
                os.environ[paths.TRACKING_URI_VARIABLE] = previous
            log.info("the company's MLflow server is stopped")
