"""A process run in a group of its own, stopped together with every process it started.

`uv run` starts the program as a child, and MLflow's server starts workers, so stopping the
first process is not enough. On POSIX the group is a new session: the terminal's Ctrl+C reaches
only the world, which stops the group with SIGTERM, then SIGKILL to whatever is left. On
Windows it is a new console process group, asked to stop with CTRL_BREAK_EVENT, inside a job
object: terminating the job kills every process in it, even once the first one has exited, and
the system terminates it when the world itself is killed outright (its handle closes).
"""

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

STOP_GRACE_SECONDS = 15.0

log = logging.getLogger(__name__)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple((name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob"))

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _checked(result: int) -> int:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    class _Job:
        """A job object holding a process and, by inheritance, everything it starts."""

        def __init__(self, pid: int) -> None:
            self._handle = _checked(_kernel32.CreateJobObjectW(None, None))
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _checked(
                _kernel32.SetInformationJobObject(
                    self._handle,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
            )
            process = _checked(
                _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, pid)
            )
            try:
                _checked(_kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                _kernel32.CloseHandle(process)

        def terminate(self) -> None:
            try:
                _checked(_kernel32.TerminateJobObject(self._handle, 1))
            finally:
                _kernel32.CloseHandle(self._handle)


class ProcessGroup:
    """`argv` started in a group of its own, its output to `output`, never through a pipe."""

    def __init__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], output: IO[bytes]
    ) -> None:
        # A file, not a pipe: over a run of hours an unread pipe would fill and block it.
        options: dict[str, object] = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": subprocess.DEVNULL,
            "stdout": output,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self.process = subprocess.Popen(list(argv), **options)  # noqa: S603 (no shell)
        self._stopped = False
        if sys.platform == "win32":
            # Assigned right after the start, before the first process has started another
            # (uv resolves its environment first); later processes join the job as they start.
            try:
                self._job = _Job(self.process.pid)
            except OSError:
                self.process.kill()
                self.process.wait()
                raise

    def exit_code(self) -> int | None:
        return self.process.poll()

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        """Ask the group to stop, wait up to `grace` seconds, then kill whatever is left."""
        if self._stopped:
            return
        self._stopped = True
        pid = self.process.pid
        try:
            if self.process.poll() is None:
                self._interrupt()
                self.process.wait(grace)
        except OSError as error:
            log.warning("cannot interrupt process %d (%s); killing it", pid, error)
        except subprocess.TimeoutExpired:
            log.warning("process %d did not stop within %g s; killing it", pid, grace)
        finally:
            # Even when a second Ctrl+C (or a signal) interrupts the wait: the guard above
            # allows no second stop, so the group is killed here or never.
            self._kill_rest(pid)

    def _kill_rest(self, pid: int) -> None:
        try:
            self._kill_everything()
        except OSError as error:
            log.error("cannot kill what is left of process %d's group: %s", pid, error)
            if self.process.poll() is None:
                self.process.kill()
        self.process.wait()

    def _interrupt(self) -> None:
        if sys.platform == "win32":
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(self.process.pid, signal.SIGTERM)

    def _kill_everything(self) -> None:
        if sys.platform == "win32":
            self._job.terminate()
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # No process of the group is left: the interrupt stopped them all.
            return
