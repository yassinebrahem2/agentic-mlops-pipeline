"""Whether the processes a test started are still running, on Windows and elsewhere."""

import os
import sys
import time
from pathlib import Path


def alive(pid: int) -> bool:
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
    # Not os.kill(pid, 0) on Windows, where 0 is CTRL_C_EVENT.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    # A zombie has exited; only its parent has not collected it yet.
    return not (stat.exists() and stat.read_text().split(")")[-1].split()[0] == "Z")


def assert_all_stopped(pids: list[int]) -> None:
    deadline = time.monotonic() + 10
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert [pid for pid in pids if alive(pid)] == []
