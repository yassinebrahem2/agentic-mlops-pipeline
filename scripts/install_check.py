"""Install a wheel in isolation and check its real CLI, UI and sign-in over HTTP."""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":

    class WindowsJob:
        """Own descendants even when a launcher exits before its children."""

        def __init__(self) -> None:
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            self.api.CreateJobObjectW.restype = wintypes.HANDLE
            self.api.OpenProcess.restype = wintypes.HANDLE
            self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.members: dict[int, int] = {}
            for name in ("CloseHandle", "TerminateJobObject"):
                getattr(self.api, name).argtypes = [wintypes.HANDLE] + (
                    [wintypes.UINT] if name == "TerminateJobObject" else []
                )
            self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.api.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            self.handle = self.api.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

        def assign(self, pid: int) -> None:
            process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.api.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.api.CloseHandle(process)

        def active(self) -> bool:
            # Retain SYNCHRONIZE handles: the job's active count can reach zero
            # before process teardown has finished and its handle is signaled.
            size = 1024
            while True:
                members = ctypes.create_string_buffer(size)
                if self.api.QueryInformationJobObject(self.handle, 3, members, size, None):
                    break
                if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise ctypes.WinError(ctypes.get_last_error())
                size *= 2
            count = int.from_bytes(members.raw[4:8], "little")
            width = ctypes.sizeof(ctypes.c_size_t)
            for offset in range(8, 8 + count * width, width):
                pid = int.from_bytes(members.raw[offset : offset + width], "little")
                if pid not in self.members:
                    handle = self.api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                    if handle:
                        self.members[pid] = handle
                    elif ctypes.get_last_error() != 87:  # Already exited and destroyed.
                        raise ctypes.WinError(ctypes.get_last_error())
            return bool(count)

        def kill(self) -> None:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            for handle in self.members.values():
                if self.api.WaitForSingleObject(handle, 10000) != 0:
                    raise RuntimeError(
                        "A Windows process has not finished exiting; temporary files are retained."
                    )
                self.api.CloseHandle(handle)
            self.members.clear()
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())


class ProcessTree:
    """Gate Windows launch until job assignment; POSIX owns a new process group."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, log: BinaryIO):
        self.job = WindowsJob() if sys.platform == "win32" else None
        # The gate prevents children escaping between Popen and job assignment.
        gated = [
            sys.executable,
            "-I",
            "-c",
            (
                "import subprocess,sys; "
                "sys.exit(subprocess.call(sys.argv[1:]) if sys.stdin.buffer.read(1) else 1)"
            ),
            *command,
        ]
        try:
            self.process = subprocess.Popen(
                gated if self.job else command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                start_new_session=sys.platform != "win32",
            )
        except OSError:
            if self.job:
                self.job.close()
            raise
        if self.job:
            try:
                self.job.assign(self.process.pid)
                if self.process.stdin is None:
                    raise RuntimeError("Cannot open the startup gate.")
                self.process.stdin.write(b"1")
                self.process.stdin.flush()
            except (OSError, RuntimeError):
                self.process.kill()
                self.process.wait()
                self.job.kill()
                self.job.close()
                raise
        if self.process.stdin:
            self.process.stdin.close()

    def active(self) -> bool:
        self.process.poll()  # Reap the direct child before checking its group.
        if self.job:
            return self.job.active()
        try:
            if sys.platform != "win32":
                os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self) -> None:
        if self.active():
            if self.job:
                # CTRL_BREAK reaches the launcher and its console descendants.
                # Some CI hosts have no console; then use the timed job kill below.
                try:
                    if sys.platform == "win32":
                        os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
                except OSError:
                    if not self.active():
                        self.process.wait()
            elif sys.platform != "win32":
                os.killpg(self.process.pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.active():
                if self.job:
                    self.job.kill()
                elif sys.platform != "win32":
                    os.killpg(self.process.pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.active():
                raise RuntimeError("Process tree did not exit; temporary files are retained.")
        self.process.wait(timeout=10)
        if self.job:
            self.job.close()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(
    port: int, path: str, *, token: str | None = None, cookie: str = "", timeout: float = 2
) -> tuple[int, dict[str, str], bytes]:
    """Direct loopback HTTP: no proxy environment, redirects or ambient cookies."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    body = None if token is None else json.dumps({"token": token})
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        response = connection.getresponse()
        values = {key.lower(): value for key, value in response.getheaders()}
        values["set-cookie"] = "; ".join(response.headers.get_all("Set-Cookie", []))
        return response.status, values, response.read()
    finally:
        connection.close()


def wait_ready(tree: ProcessTree, port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tree.process.poll() is not None:
            raise RuntimeError(
                f"Installed command exited with code {tree.process.returncode} before readiness."
            )
        try:
            status, _, _ = request(
                port, "/api/auth/status", timeout=min(1, deadline - time.monotonic())
            )
            if status == 200:
                return
        except (OSError, http.client.HTTPException):
            # Connection refusal/disconnect is expected while uvicorn starts.
            pass
        time.sleep(0.1)
    raise RuntimeError("/api/auth/status did not return 200 within 30 seconds.")


class Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.built = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        source = dict(attrs).get("src", "") or ""
        if tag == "script" and source.startswith("/assets/") and source.endswith(".js"):
            self.built = True


def check_app(port: int, token: str) -> None:
    status, headers, html = request(port, "/")
    scripts = Scripts()
    scripts.feed(html.decode("utf-8"))
    if status != 200 or "text/html" not in headers.get("content-type", "") or not scripts.built:
        raise RuntimeError("/ must return 200 HTML containing the built UI script tag.")
    status, headers, deep = request(port, "/projects/prj_x/agents")
    if status != 200 or "text/html" not in headers.get("content-type", "") or deep != html:
        raise RuntimeError("The project deep link did not return the same UI HTML.")
    status, headers, _ = request(port, "/api/auth/token", token=token)
    if status != 200:
        raise RuntimeError(f"Token sign-in returned {status}; expected 200.")
    cookies = SimpleCookie()
    cookies.load(headers.get("set-cookie", ""))
    session = cookies.get("session")
    if session is None or not session.value or not session["httponly"]:
        raise RuntimeError("Token sign-in did not set an HttpOnly session cookie.")
    status, _, body = request(port, "/api/auth/status", cookie=f"session={session.value}")
    if status != 200 or json.loads(body).get("signed_in") is not True:
        raise RuntimeError("The issued session cookie did not sign in.")
    status, _, _ = request(port, "/api/projects")
    if status != 401:
        raise RuntimeError(f"Guarded route without a cookie returned {status}; expected 401.")


def isolated_environment(root: Path, token: str) -> dict[str, str]:
    # Do not pass provider credentials, user app configuration or Python overrides.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(
        MLOPS_AGENTS_HOME=str(root / "data"),
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        MLOPS_AGENTS_ACCESS_TOKEN=token,
        PYTHONUNBUFFERED="1",
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        print("uv was not found on PATH. Install uv.", file=sys.stderr)
        return 1
    wheel = args.wheel.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        print("Supply an existing wheel built by scripts/build_wheel.py.", file=sys.stderr)
        return 1
    root = Path(tempfile.mkdtemp(prefix="mlops-install-check-"))
    token = secrets.token_urlsafe(32)
    env = isolated_environment(root, token)
    tree: ProcessTree | None = None
    failure: str | None = None
    stopped = True
    with (root / "output.log").open("w+b") as log:
        try:
            result = subprocess.run(
                [uv, "--no-config", "tool", "install", str(wheel)],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=300,
            )
            if result.returncode:
                raise RuntimeError(f"uv tool install failed with exit code {result.returncode}.")
            executable = root / "bin" / ("mlops-agents.exe" if os.name == "nt" else "mlops-agents")
            port = free_port()
            tree = ProcessTree(
                [str(executable), "start", "--no-browser", "--port", str(port)], env, root, log
            )
            wait_ready(tree, port)
            check_app(port, token)
        except (
            OSError,
            ValueError,
            RuntimeError,
            http.client.HTTPException,
            subprocess.SubprocessError,
        ) as error:
            failure = str(error)
        finally:
            if tree is not None:
                try:
                    tree.stop()
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    stopped = False
                    failure = f"{failure or ''}\nTeardown failed: {error}"
            if failure:
                log.seek(0)
                # No inherited credentials enter the child. The generated token can
                # legitimately appear in the CLI's printed access URL.
                output = log.read().decode("utf-8", errors="replace").replace(token, "[REDACTED]")
                print(
                    f"Install check failed: {failure.replace(token, '[REDACTED]')}\n{output}",
                    file=sys.stderr,
                )
    if stopped:
        shutil.rmtree(root)
    else:
        print(f"Retained temporary directory: {root}", file=sys.stderr)
    if failure:
        return 1
    print("Install check passed: UI, deep link, sign-in and unauthorized access.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
