"""A fake OpenAI-compatible server: deterministic, streaming, with scripted failures.

Used by backend tests (fixture `fake_openai`) and by the end-to-end test, which registers it
as an ordinary self-hosted provider. Run it alone with: python -m tests.fakes.fake_openai --port N
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import socket
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# Pyright checks tests/fakes as its own root (pyproject's executionEnvironments).
if TYPE_CHECKING:
    from onboarding_script import audit_reply, onboarding_reply
else:
    from tests.fakes.onboarding_script import audit_reply, onboarding_reply

MODELS: dict[str, int] = {
    "fake-tools": 32768,
    "fake-no-tools": 8192,
    "fake-slow": 32768,
    "fake-invalid-tools": 32768,
    "fake-mixed-tools": 32768,
    "fake-write": 32768,
    "fake-onboarding": 131072,
    "fake-audit": 131072,
}
EMBEDDING_MODELS: dict[str, int] = {
    "fake-embed": 16,
    "fake-embed-wide": 32,
}


def fake_embedding(text: str, dimensions: int) -> list[float]:
    words = re.findall(r"\w+", text.lower())
    vec = [0.0] * dimensions
    for w in words:
        digest = hashlib.sha256(w.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


TOOL_ARGS: dict[str, dict[str, Any]] = {
    "Read": {"file_path": "README.md"},
    "report_capabilities": {"ok": True},
    "StartTask": {"title": "Write notes"},
    "Write": {"file_path": "NOTES.md", "content": "# Notes\n\nNotes created by fake-write.\n"},
}
SLOW_TEXT = " ".join(f"word{i}" for i in range(40))
# One chat request's answer for a key: "ok" serves it as usual; ("429", retry_after) is a rate
# limit; "429-quota-wording" a per-minute limit worded as a quota; "402" and
# "insufficient_quota" (a 429) billing; "401" a rejected key.
KeyOutcome = (
    Literal["ok", "429-quota-wording", "402", "insufficient_quota", "401"]
    | tuple[Literal["429"], float | None]
)


def _open_gate() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


@dataclass
class FakeState:
    required_key: str | None = None
    rate_limit_remaining: int = 0
    server_error_remaining: int = 0
    auth_fail: bool = False
    usage_in_stream: bool = True
    # Per-token USD strings, as OpenRouter's /models publishes them, by model id.
    pricing: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    drop_after_chunks: int | None = None
    scripted_tool_calls: list[dict[str, Any]] | None = None
    bad_generation_remaining: int = 0
    bad_generation_code: str = "tool_use_failed"
    bad_generation_message: str = "Tool call validation failed"
    sse_error_remaining: int = 0
    sse_error_code: str = "tool_use_failed"
    sse_error_message: str = "Tool call validation failed in stream"
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    request_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    key_scripts: dict[str, list[KeyOutcome]] = field(default_factory=dict[str, list[KeyOutcome]])
    # Embeddings
    embedding_wrong_dimensions: int | None = None
    embedding_truncate_count: int | None = None
    embedding_error_status: int | None = None
    embedding_error_message: str | None = None
    embedding_error_code: str = "bad_request"
    _slow_gate: threading.Event = field(default_factory=_open_gate)

    def script_for_key(self, key: str, outcomes: list[KeyOutcome]) -> None:
        """Answer the next chat requests made with `key`, one outcome each, in order; once they
        are used up the key is served as usual."""
        self.key_scripts[key] = list(outcomes)

    def hold_slow(self) -> None:
        self._slow_gate.clear()

    def release_slow(self) -> None:
        self._slow_gate.set()

    def reset(self) -> None:
        self.release_slow()
        self.required_key = None
        self.rate_limit_remaining = 0
        self.server_error_remaining = 0
        self.auth_fail = False
        self.usage_in_stream = True
        self.pricing.clear()
        self.drop_after_chunks = None
        self.scripted_tool_calls = None
        self.bad_generation_remaining = 0
        self.bad_generation_code = "tool_use_failed"
        self.bad_generation_message = "Tool call validation failed"
        self.sse_error_remaining = 0
        self.sse_error_code = "tool_use_failed"
        self.sse_error_message = "Tool call validation failed in stream"
        self.embedding_wrong_dimensions = None
        self.embedding_truncate_count = None
        self.embedding_error_status = None
        self.embedding_error_message = None
        self.embedding_error_code = "bad_request"
        self.requests.clear()
        self.request_headers.clear()
        self.key_scripts.clear()


def _scripted_response(state: FakeState, request: Request) -> Response | None:
    """The scripted answer for the request's bearer key, or None to serve it as usual."""
    authorization = request.headers.get("authorization", "")
    script = state.key_scripts.get(authorization.removeprefix("Bearer "))
    if not script:
        return None
    outcome = script.pop(0)
    if outcome == "ok":
        return None
    if outcome == "429-quota-wording":
        return _error(429, "You exceeded your current quota", "rate_limit_exceeded")
    if outcome == "402":
        return _error(402, "Payment required", "payment_required")
    if outcome == "insufficient_quota":
        return _error(429, "You exceeded your current quota", "insufficient_quota")
    if outcome == "401":
        return _error(401, "Invalid API key", "invalid_api_key")
    _, retry_after = outcome
    headers = {"retry-after": str(retry_after)} if retry_after is not None else None
    return _error(429, "Rate limit reached", "rate_limit_exceeded", headers)


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
    state: FakeState,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]] | None]:
    """(text, list of tool calls to make or None)."""
    last = messages[-1]
    if model == "fake-no-tools" or not tools:
        return "Hello from the fake model.", None
    if any(tool["function"]["name"] == "report_capabilities" for tool in tools):
        return "", [{"name": "report_capabilities", "arguments": TOOL_ARGS["report_capabilities"]}]
    if model == "fake-onboarding":
        return onboarding_reply(messages, tools)
    if model == "fake-audit":
        return audit_reply(messages), None
    if model == "fake-slow":
        return SLOW_TEXT, None
    if model == "fake-write":
        # Only the user's own message can be the PR reminder; a tool result may mention PRs too.
        user_text = str(last.get("content") or "") if last.get("role") == "user" else ""
        if "pull request" in user_text or "OpenPullRequest" in user_text:
            return (
                "This project is local with no remote repository, so no pull request is needed.",
                None,
            )
        if last.get("role") == "tool":
            tool_name = str(last.get("name") or "")
            if not tool_name:
                tool_call_id = str(last.get("tool_call_id") or "")
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        tool_calls_list: list[dict[str, Any]] = m.get("tool_calls") or []
                        for tc in tool_calls_list:
                            if tc.get("id") == tool_call_id:
                                fn: dict[str, Any] = tc.get("function") or {}
                                tool_name = str(fn.get("name") or tc.get("name") or "")
                                break
                        if tool_name:
                            break
            if tool_name == "StartTask":
                return "", [{"name": "Write", "arguments": TOOL_ARGS["Write"]}]
            if tool_name == "Write":
                return "I have started the task and written NOTES.md.", None
            return "Done.", None
        return "", [{"name": "StartTask", "arguments": TOOL_ARGS["StartTask"]}]
    if last.get("role") == "tool":
        content = str(last.get("content") or "").strip()
        first_line = content.splitlines()[0] if content else "(empty)"
        return f"Done. First line: {first_line}", None
    if state.scripted_tool_calls is not None:
        return "", list(state.scripted_tool_calls)
    if model == "fake-invalid-tools":
        return "", [{"name": str(tools[0]["function"]["name"]), "arguments": "{pattern: foo}"}]
    if model == "fake-mixed-tools":
        t0 = str(tools[0]["function"]["name"])
        t1 = str(tools[1]["function"]["name"]) if len(tools) > 1 else t0
        return "", [
            {"name": t0, "arguments": TOOL_ARGS.get(t0, {})},
            {"name": t1, "arguments": "{pattern: foo}"},
        ]
    return "", [
        {
            "name": str(tools[0]["function"]["name"]),
            "arguments": TOOL_ARGS.get(str(tools[0]["function"]["name"]), {}),
        }
    ]


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

    text, tool_calls = _reply(state, model, messages, tools)
    maybe_drop()
    yield _chunk(model, {"role": "assistant", "content": ""})
    if state.sse_error_remaining > 0:
        state.sse_error_remaining -= 1
        sse_err = {
            "error": {
                "message": state.sse_error_message,
                "type": "invalid_request_error",
                "code": state.sse_error_code,
            }
        }
        yield f"event: error\ndata: {json.dumps(sse_err)}\n\n"
        return
    if tool_calls is not None:
        num_calls = len(tool_calls)
        for idx, tc in enumerate(tool_calls):
            tool = tc["name"]
            raw_args = tc.get("arguments", {})
            args = json.dumps(raw_args) if isinstance(raw_args, dict) else str(raw_args)
            half = len(args) // 2
            call_id = tc.get("id") or (
                f"call_{len(state.requests)}_{idx}"
                if num_calls > 1
                else f"call_{len(state.requests)}"
            )
            first = {
                "index": idx,
                "id": call_id,
                "type": "function",
                "function": {"name": tool, "arguments": args[:half]},
            }
            maybe_drop()
            yield _chunk(model, {"tool_calls": [first]})
            maybe_drop()
            yield _chunk(
                model,
                {"tool_calls": [{"index": idx, "function": {"arguments": args[half:]}}]},
            )
        finish = "tool_calls"
    else:
        for i, word in enumerate(text.split(" ")):
            if model == "fake-slow":
                await asyncio.sleep(0.05)
            maybe_drop()
            yield _chunk(model, {"content": word if i == 0 else " " + word})
            if model == "fake-slow" and i == 0:
                while not state._slow_gate.is_set():
                    await asyncio.sleep(0.02)
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


def _is_authorized(state: FakeState, request: Request) -> bool:
    if state.auth_fail:
        return False
    return state.required_key is None or (
        request.headers.get("authorization") == f"Bearer {state.required_key}"
    )


def embeddings_failure(state: FakeState, request: Request) -> Response | None:
    if not _is_authorized(state, request):
        auth_hdr = request.headers.get("authorization") or ""
        msg = f"Invalid API key: {auth_hdr}" if auth_hdr else "Invalid API key"
        return _error(401, msg, "invalid_api_key")
    if state.embedding_error_status is not None:
        status = state.embedding_error_status
        msg = state.embedding_error_message or "Embeddings error"
        code = state.embedding_error_code
        state.embedding_error_status = None
        return _error(status, msg, code)
    if state.rate_limit_remaining > 0:
        state.rate_limit_remaining -= 1
        return _error(429, "Rate limit reached", "rate_limit_exceeded", {"retry-after": "0"})
    if state.server_error_remaining > 0:
        state.server_error_remaining -= 1
        return _error(503, "The model is overloaded", "overloaded")
    return None


def create_fake_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    def authorized(request: Request) -> bool:
        return _is_authorized(state, request)

    async def list_models(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        data: list[dict[str, Any]] = [
            {"id": m, "object": "model", "context_window": c} for m, c in MODELS.items()
        ] + [{"id": m, "object": "model"} for m in EMBEDDING_MODELS]
        for entry in data:
            if entry["id"] in state.pricing:
                entry["pricing"] = state.pricing[str(entry["id"])]
        return JSONResponse({"object": "list", "data": data})

    async def chat(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        body: dict[str, Any] = await request.json()
        state.requests.append(body)
        state.request_headers.append(dict(request.headers))
        scripted = _scripted_response(state, request)
        if scripted is not None:
            return scripted
        if state.rate_limit_remaining > 0:
            state.rate_limit_remaining -= 1
            return _error(429, "Rate limit reached", "rate_limit_exceeded", {"retry-after": "0"})
        if state.server_error_remaining > 0:
            state.server_error_remaining -= 1
            return _error(503, "The model is overloaded", "overloaded")
        if state.bad_generation_remaining > 0:
            state.bad_generation_remaining -= 1
            body_obj = {
                "error": {
                    "message": state.bad_generation_message,
                    "type": "invalid_request_error",
                    "code": state.bad_generation_code,
                    "failed_generation": "...",
                }
            }
            return JSONResponse(body_obj, status_code=400)
        model = str(body["model"])
        if model not in MODELS:
            return _error(404, f"Unknown model {model}", "model_not_found")
        options: dict[str, Any] = body.get("stream_options") or {}
        usage = bool(options.get("include_usage")) and state.usage_in_stream
        stream = _stream(state, model, body["messages"], body.get("tools") or [], usage)
        return StreamingResponse(stream, media_type="text/event-stream")

    async def embeddings(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        body: dict[str, Any] = await request.json()
        state.requests.append(body)
        state.request_headers.append(dict(request.headers))
        failure = embeddings_failure(state, request)
        if failure is not None:
            return failure
        model = str(body.get("model", ""))
        if model not in EMBEDDING_MODELS:
            return _error(404, f"Unknown model {model}", "model_not_found")
        raw_input: object = body.get("input", "")
        if isinstance(raw_input, str):
            inputs = [raw_input]
        elif isinstance(raw_input, list):
            raw_list = cast(list[object], raw_input)
            inputs = [str(x) for x in raw_list]
        else:
            inputs = [str(raw_input)]
        dim = (
            state.embedding_wrong_dimensions
            if state.embedding_wrong_dimensions is not None
            else EMBEDDING_MODELS[model]
        )
        data = [
            {
                "object": "embedding",
                "index": i,
                "embedding": fake_embedding(text, dim),
            }
            for i, text in enumerate(inputs)
        ]
        if state.embedding_truncate_count is not None:
            data = data[: state.embedding_truncate_count]
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "model": model,
                "usage": {
                    "prompt_tokens": len(inputs) * 10,
                    "total_tokens": len(inputs) * 10,
                },
            }
        )

    app.add_api_route("/v1/models", list_models, methods=["GET"])
    app.add_api_route("/v1/chat/completions", chat, methods=["POST"])
    app.add_api_route("/v1/embeddings", embeddings, methods=["POST"])

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
