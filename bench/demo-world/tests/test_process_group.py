"""A process group is stopped whole, even when the stop itself is interrupted."""

import os
import sys
import time
from pathlib import Path

import pytest

from demo_world.process_group import ProcessGroup
from tests.processes import alive, assert_all_stopped


def _stubborn(tmp_path: Path) -> tuple[ProcessGroup, list[int]]:
    """A process that ignores the stop signal, and the interpreter it runs as (its own pid)."""
    pid_file = tmp_path / "pid.txt"
    code = (
        "import os, pathlib, signal, time; "
        "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(600)"
    )
    with (tmp_path / "output.log").open("wb") as output:
        group = ProcessGroup([sys.executable, "-c", code], tmp_path, os.environ, output)
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pids = [group.process.pid, int(pid_file.read_text())]
    assert [alive(pid) for pid in pids] == [True, True]
    return group, pids


def test_a_second_ctrl_c_during_the_stop_still_kills_the_group(tmp_path: Path) -> None:
    group, pids = _stubborn(tmp_path)
    wait = group.process.wait

    def interrupted_wait(timeout: float | None = None) -> int:
        # The user presses Ctrl+C again while the world waits for the group to stop.
        if timeout is not None:
            raise KeyboardInterrupt
        return wait()

    group.process.wait = interrupted_wait

    with pytest.raises(KeyboardInterrupt):
        group.stop(grace=5)

    assert_all_stopped(pids)


def test_a_stubborn_process_is_killed_after_the_grace(tmp_path: Path) -> None:
    group, pids = _stubborn(tmp_path)

    group.stop(grace=0.5)

    assert_all_stopped(pids)
    assert group.exit_code() is not None
