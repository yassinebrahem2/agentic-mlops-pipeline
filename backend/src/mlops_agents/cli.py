"""Command-line interface for mlops-agents."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import errno
import os
import socket
import sys
import webbrowser
from collections.abc import Callable, Sequence

import uvicorn

from mlops_agents.api.app import create_app
from mlops_agents.config import DEFAULT_PORT, ConfigError, RunMode, load_settings
from mlops_agents.credentials import KeyStoreError, open_keystore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.backup import snapshot_all

# Linux/macOS raises EADDRINUSE; Windows raises WSAEADDRINUSE (10048) or WSAEACCES (10013 / EACCES).
IN_USE_ERRNOS: set[int] = {errno.EADDRINUSE, 10048, 10013}
if sys.platform == "win32":
    IN_USE_ERRNOS.add(errno.EACCES)


async def _run_server(
    server: uvicorn.Server, sock: socket.socket, callback: Callable[[], None]
) -> None:
    called = False

    def on_started() -> None:
        nonlocal called
        if not called:
            called = True
            callback()

    async def poll() -> None:
        while not server.started and not server.should_exit:  # noqa: ASYNC110 - third-party uvicorn.Server exposes only the .started attribute, not an asyncio.Event
            await asyncio.sleep(0.01)
        if server.started:
            on_started()

    task = asyncio.create_task(poll())
    try:
        await server.serve(sockets=[sock])
    finally:
        if server.started:
            on_started()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _start(args: argparse.Namespace) -> int:
    if args.dev and args.server:
        print("--dev is for local mode only.", file=sys.stderr)
        sys.exit(2)

    mode = RunMode.SERVER if args.server else RunMode.LOCAL
    try:
        settings = load_settings(mode, port=args.port)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    if args.dev:
        dev_origins = tuple(
            dict.fromkeys((*settings.dev_origins, "http://localhost:5173", "http://127.0.0.1:5173"))
        )
        settings = dataclasses.replace(settings, dev_origins=dev_origins)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "posix":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((settings.host, settings.port))
    except OSError as exc:
        sock.close()
        if exc.errno in IN_USE_ERRNOS:
            print(
                f"Port {settings.port} is in use. Start with --port and another number, for example --port {settings.port + 1}.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    try:
        keystore = open_keystore(settings, DataPaths(settings.data_dir))
    except (ConfigError, KeyStoreError) as exc:
        sock.close()
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    app = create_app(settings, keystore=keystore)
    access_token: str = app.state.access_token

    def notify_started() -> None:
        if settings.mode is RunMode.LOCAL:
            url = (
                f"http://localhost:5173/#token={access_token}"
                if args.dev
                else f"http://127.0.0.1:{settings.port}/#token={access_token}"
            )
            print(f"Open {url}")
            if not args.no_browser:
                webbrowser.open(url)
        else:
            print(f"Listening on {settings.host}:{settings.port}")

    config = uvicorn.Config(app, log_level="info", timeout_graceful_shutdown=5, proxy_headers=False)
    server = uvicorn.Server(config)
    try:
        asyncio.run(_run_server(server, sock, notify_started))
    finally:
        sock.close()
    return 0


def _backup() -> int:
    try:
        settings = load_settings(RunMode.LOCAL)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    paths = DataPaths(settings.data_dir)
    paths.ensure()
    dest = snapshot_all(paths)
    print(str(dest))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlops-agents")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument(
        "--server",
        action="store_true",
        help="run in server mode",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default: {DEFAULT_PORT})",
    )
    start_parser.add_argument(
        "--dev",
        action="store_true",
        help="development mode: allow Vite dev server origins (local mode only)",
    )
    start_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser window",
    )

    subparsers.add_parser("backup")

    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            return _start(args)
        if args.command == "backup":
            return _backup()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
