"""Stub model: an OpenAI-compatible chat-completions endpoint on 127.0.0.1."""

from __future__ import annotations

import collections.abc
import json
import socket
import threading
import time
from collections.abc import Sequence
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from benchmarks.client import BenchClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_stub_app(model_name: str = "stub-model") -> FastAPI:
    """Builds a lightweight FastAPI app implementing OpenAI models and chat completions."""
    app = FastAPI(title="stub-model")

    async def list_models() -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": model_name, "object": "model", "context_window": 128000}],
            }
        )

    async def chat_completions(request: Request) -> Any:
        try:
            raw_body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        if not isinstance(raw_body, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object body")
        body: dict[str, Any] = cast(dict[str, Any], raw_body)
        req_model = str(body.get("model", model_name))
        is_stream = bool(body.get("stream", False))

        # Check if this is the probe request during verification
        raw_tools: object = body.get("tools")
        is_probe = False
        if isinstance(raw_tools, Sequence):
            tools_seq = cast(Sequence[object], raw_tools)
            for t in tools_seq:
                if isinstance(t, dict):
                    fn_obj: object = cast(dict[str, Any], t).get("function")
                    if isinstance(fn_obj, dict):
                        fn_name: object = cast(dict[str, Any], fn_obj).get("name")
                        if fn_name == "report_capabilities":
                            is_probe = True
                            break

        if is_stream:

            async def event_generator() -> collections.abc.AsyncIterator[str]:
                if is_probe:
                    # Probe chunk 1: tool call
                    chunk1: dict[str, Any] = {
                        "id": "chatcmpl-stub-probe",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": req_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_probe_1",
                                            "type": "function",
                                            "function": {
                                                "name": "report_capabilities",
                                                "arguments": '{"ok": true}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk1)}\n\n"
                    # Probe chunk 2: stop
                    chunk2: dict[str, Any] = {
                        "id": "chatcmpl-stub-probe",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": req_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk2)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # Normal turn chunk 1: message content
                chunk1 = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "The turn has concluded without tool calls.",
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk1)}\n\n"
                # Normal turn chunk 2: stop
                chunk2 = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk2)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_generator(), media_type="text/event-stream")

        if is_probe:
            return JSONResponse(
                {
                    "id": "chatcmpl-stub-probe",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_probe_1",
                                        "type": "function",
                                        "function": {
                                            "name": "report_capabilities",
                                            "arguments": '{"ok": true}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            )

        return JSONResponse(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "The turn has concluded without tool calls.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )

    app.add_api_route("/v1/models", list_models, methods=["GET"])
    app.add_api_route("/models", list_models, methods=["GET"])
    app.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])
    app.add_api_route("/chat/completions", chat_completions, methods=["POST"])

    return app


class StubModelServer:
    """Running stub model server."""

    def __init__(
        self,
        port: int,
        server: uvicorn.Server,
        thread: threading.Thread,
        model_name: str = "stub-model",
    ) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.model_name = model_name
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)


def start_stub_model(
    model_name: str = "stub-model",
    port: int | None = None,
) -> StubModelServer:
    """Starts the stub OpenAI server on 127.0.0.1."""
    actual_port = port or _free_port()
    app = create_stub_app(model_name)
    config = uvicorn.Config(app, host="127.0.0.1", port=actual_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Stub model server failed to start within 10s")
        time.sleep(0.02)
    return StubModelServer(actual_port, server, thread, model_name)


async def register_stub_model(
    client: BenchClient,
    stub: StubModelServer,
    provider_name: str = "stub",
) -> str:
    """Registers the stub model with the app as a self_hosted provider."""
    # 1. POST /providers
    await client.post(
        "/providers",
        body={
            "name": provider_name,
            "kind": "self_hosted",
            "base_url": stub.base_url,
        },
    )
    # 2. POST /models
    raw_model_res: object = await client.post(
        "/models",
        body={
            "provider": provider_name,
            "wire_model_id": stub.model_name,
            "context_window": 128000,
        },
    )
    if not isinstance(raw_model_res, dict):
        raise ValueError(f"Expected dict response from /models, got {type(raw_model_res).__name__}")
    model_dict: dict[str, Any] = cast(dict[str, Any], raw_model_res)
    raw_addr: object = model_dict.get("address")
    if not raw_addr or not isinstance(raw_addr, str):
        raise ValueError(f"Missing or invalid 'address' in /models response: {raw_model_res}")
    address = raw_addr
    # 3. POST /models/verify
    await client.post("/models/verify", body={"address": address})
    return address
