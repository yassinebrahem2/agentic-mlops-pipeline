"""A fake Discord webhook server for tests.

Follows fake_github.py: a FastAPI app with recorded posts, scripted failures (status codes, 429),
start_fake_discord, and python -m tests.fakes.fake_discord --port N.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


@dataclass
class FakeDiscordState:
    posts: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    failures: list[int | tuple[int, str]] = field(
        default_factory=lambda: list[int | tuple[int, str]]()
    )

    def fail_next(self, status_code: int, body: str | None = None) -> None:
        if body is not None:
            self.failures.append((status_code, body))
        else:
            self.failures.append(status_code)

    def reset(self) -> None:
        self.posts.clear()
        self.failures.clear()


def create_fake_discord_app(state: FakeDiscordState) -> FastAPI:
    app = FastAPI(title="Fake Discord Webhook")

    @app.post("/{path:path}")
    async def post_webhook(path: str, request: Request) -> Response:
        if state.failures:
            failure_item = state.failures.pop(0)
            if isinstance(failure_item, tuple):
                status_code, custom_body = failure_item
                return Response(
                    content=custom_body,
                    status_code=status_code,
                    media_type="text/plain",
                )
            status_code = failure_item
            if status_code == 429:
                return JSONResponse(
                    {"message": "You are being rate limited.", "retry_after": 5.0},
                    status_code=429,
                )
            return JSONResponse(
                {"message": f"Discord error status {status_code}"},
                status_code=status_code,
            )

        body: Any = None
        try:
            body = await request.json()
        except Exception:
            body = (await request.body()).decode("utf-8", errors="replace")

        body_dict: dict[str, Any] = cast(dict[str, Any], body) if isinstance(body, dict) else {}
        post_data: dict[str, Any] = {
            "path": f"/{path}",
            "headers": dict(request.headers),
            "content": body_dict.get("content"),
            "allowed_mentions": body_dict.get("allowed_mentions"),
            "json": body if isinstance(body, dict) else None,
            "raw": body,
        }
        state.posts.append(post_data)
        return Response(status_code=204)

    return app


@dataclass
class RunningFakeDiscord:
    base_url: str
    state: FakeDiscordState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_discord(state: FakeDiscordState, port: int = 0) -> RunningFakeDiscord:
    port = port or _free_port()
    config = uvicorn.Config(
        create_fake_discord_app(state),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake Discord server did not start within 10 seconds")
        time.sleep(0.02)
    return RunningFakeDiscord(f"http://127.0.0.1:{port}", state, server, thread)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Discord webhook server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_fake_discord_app(FakeDiscordState()),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
