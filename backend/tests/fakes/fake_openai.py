"""A fake OpenAI-compatible server: deterministic, streaming, with scripted failures.

Used by backend tests (fixture `fake_openai`) and by the end-to-end test, which registers it
as an ordinary self-hosted provider. Run it alone with: python -m tests.fakes.fake_openai --port N
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

MODELS: dict[str, int] = {"fake-tools": 32768, "fake-no-tools": 8192, "fake-slow": 32768}
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "Read": {"file_path": "README.md"},
    "report_capabilities": {"ok": True},
}
SLOW_TEXT = " ".join(f"word{i}" for i in range(40))


@dataclass
class FakeState:
    required_key: str | None = None
    rate_limit_remaining: int = 0
    server_error_remaining: int = 0
    auth_fail: bool = False
    usage_in_stream: bool = True
    drop_after_chunks: int | None = None
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    request_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def reset(self) -> None:
        self.required_key = None
        self.rate_limit_remaining = 0
        self.server_error_remaining = 0
        self.auth_fail = False
        self.usage_in_stream = True
        self.drop_after_chunks = None
        self.requests.clear()
        self.request_headers.clear()


def _error(status: int, message: str, code: str, headers: dict[str, str] | None = None) -> Response:
    body = {"error": {"message": message, "type": "fake_error", "code": code}}
    return JSONResponse(body, status_code=status, headers=headers)


def _chunk(model: str, delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _reply(
    model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """(text, name of the tool to call or None)."""
    last = messages[-1]
    if model == "fake-slow" and any(
        tool["function"]["name"] == "report_capabilities" for tool in tools
    ):
        return "", "report_capabilities"
    if model == "fake-slow":
        return SLOW_TEXT, None
    if model == "fake-no-tools" or not tools:
        return "Hello from the fake model.", None
    if last.get("role") == "tool":
        content = str(last.get("content") or "").strip()
        first_line = content.splitlines()[0] if content else "(empty)"
        return f"Done. First line: {first_line}", None
    return "", str(tools[0]["function"]["name"])


async def _stream(
    state: FakeState,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    usage: bool,
) -> AsyncIterator[str]:
    yielded = 0

    def maybe_drop() -> None:
        nonlocal yielded
        if state.drop_after_chunks is not None and yielded >= state.drop_after_chunks:
            raise RuntimeError("fake connection drop")
        yielded += 1

    text, tool = _reply(model, messages, tools)
    maybe_drop()
    yield _chunk(model, {"role": "assistant", "content": ""})
    if tool is not None:
        args = json.dumps(TOOL_ARGS.get(tool, {}))
        half = len(args) // 2
        call_id = f"call_{len(state.requests)}"
        first = {
            "index": 0,
            "id": call_id,
            "type": "function",
            "function": {"name": tool, "arguments": args[:half]},
        }
        maybe_drop()
        yield _chunk(model, {"tool_calls": [first]})
        maybe_drop()
        yield _chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]})
        finish = "tool_calls"
    else:
        for i, word in enumerate(text.split(" ")):
            if model == "fake-slow":
                await asyncio.sleep(0.05)
            maybe_drop()
            yield _chunk(model, {"content": word if i == 0 else " " + word})
        finish = "stop"
    maybe_drop()
    yield _chunk(model, {}, finish)
    if usage:
        tail: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        maybe_drop()
        yield f"data: {json.dumps(tail)}\n\n"
    maybe_drop()
    yield "data: [DONE]\n\n"


def create_fake_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    def authorized(request: Request) -> bool:
        if state.auth_fail:
            return False
        return state.required_key is None or (
            request.headers.get("authorization") == f"Bearer {state.required_key}"
        )

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        data = [{"id": m, "object": "model", "context_window": c} for m, c in MODELS.items()]
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        body: dict[str, Any] = await request.json()
        state.requests.append(body)
        state.request_headers.append(dict(request.headers))
        if state.rate_limit_remaining > 0:
            state.rate_limit_remaining -= 1
            return _error(429, "Rate limit reached", "rate_limit_exceeded", {"retry-after": "0"})
        if state.server_error_remaining > 0:
            state.server_error_remaining -= 1
            return _error(503, "The model is overloaded", "overloaded")
        model = str(body["model"])
        if model not in MODELS:
            return _error(404, f"Unknown model {model}", "model_not_found")
        options: dict[str, Any] = body.get("stream_options") or {}
        usage = bool(options.get("include_usage")) and state.usage_in_stream
        stream = _stream(state, model, body["messages"], body.get("tools") or [], usage)
        return StreamingResponse(stream, media_type="text/event-stream")

    return app


@dataclass
class RunningFake:
    base_url: str
    state: FakeState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_server(state: FakeState, port: int = 0) -> RunningFake:
    port = port or _free_port()
    config = uvicorn.Config(
        create_fake_app(state), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake OpenAI server did not start within 10 seconds")
        time.sleep(0.02)
    return RunningFake(f"http://127.0.0.1:{port}/v1", state, server, thread)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OpenAI-compatible server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(create_fake_app(FakeState()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
