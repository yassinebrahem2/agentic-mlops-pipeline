from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest

from mlops_agents.providers.verification import probe_model

if TYPE_CHECKING:
    from fake_openai import FakeState, RunningFake, start_fake_server
else:
    from tests.fakes.fake_openai import FakeState, RunningFake, start_fake_server


@pytest.fixture
def fake_server() -> Iterator[RunningFake]:
    state = FakeState()
    running = start_fake_server(state)
    try:
        yield running
    finally:
        running.stop()


async def _stream_chunks(
    client: httpx2.AsyncClient, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ").strip()
            if data == "[DONE]":
                break
            chunks.append(cast(dict[str, Any], json.loads(data)))
    return chunks


async def test_fake_openai_models_includes_fake_write(fake_server: RunningFake) -> None:
    async with httpx2.AsyncClient(base_url=fake_server.base_url.removesuffix("/v1")) as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data: dict[str, Any] = resp.json()
        models_list: list[dict[str, Any]] = data["data"]
        model_ids = [m["id"] for m in models_list]
        assert "fake-write" in model_ids


async def test_probe_fake_write_succeeds(fake_server: RunningFake) -> None:
    async with httpx2.AsyncClient() as client:
        res = await probe_model(fake_server.base_url, None, "fake-write", http=client)
        assert res.tool_calling
        assert res.streaming
        assert res.detail is None


async def test_fake_write_lifecycle_steps(fake_server: RunningFake) -> None:
    async with httpx2.AsyncClient(base_url=fake_server.base_url.removesuffix("/v1")) as client:
        # Step 0: Verification probe (report_capabilities)
        probe_chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": [{"role": "user", "content": "Probe"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "report_capabilities",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )
        tool_calls: list[str] = []
        for c in probe_chunks:
            choices = cast(list[dict[str, Any]], c.get("choices") or [])
            if not choices:
                continue
            delta = cast(dict[str, Any], choices[0].get("delta") or {})
            tcs = cast(list[dict[str, Any]], delta.get("tool_calls") or [])
            if tcs:
                fn = cast(dict[str, Any], tcs[0].get("function") or {})
                if "name" in fn:
                    tool_calls.append(str(fn["name"]))
        assert tool_calls == ["report_capabilities"]

        # Step 1: User asks -> StartTask tool call
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Please start a task and write notes."}
        ]
        s1_chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": messages,
                "tools": [
                    {"type": "function", "function": {"name": "StartTask", "parameters": {}}},
                    {"type": "function", "function": {"name": "Write", "parameters": {}}},
                ],
            },
        )
        tc_names: list[str] = []
        for c in s1_chunks:
            choices = cast(list[dict[str, Any]], c.get("choices") or [])
            if not choices:
                continue
            delta = cast(dict[str, Any], choices[0].get("delta") or {})
            tcs = cast(list[dict[str, Any]], delta.get("tool_calls") or [])
            if tcs:
                fn = cast(dict[str, Any], tcs[0].get("function") or {})
                if "name" in fn:
                    tc_names.append(str(fn["name"]))
        assert tc_names == ["StartTask"]

        # Step 2: Tool result for StartTask arrives -> Write tool call on NOTES.md
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_start",
                            "type": "function",
                            "function": {
                                "name": "StartTask",
                                "arguments": '{"title": "Write notes"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_start",
                    "content": '{"id": "task_1", "title": "Write notes"}',
                },
            ]
        )
        s2_chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": messages,
                "tools": [
                    {"type": "function", "function": {"name": "StartTask", "parameters": {}}},
                    {"type": "function", "function": {"name": "Write", "parameters": {}}},
                ],
            },
        )
        tc2_names: list[str] = []
        for c in s2_chunks:
            choices = cast(list[dict[str, Any]], c.get("choices") or [])
            if not choices:
                continue
            delta = cast(dict[str, Any], choices[0].get("delta") or {})
            tcs = cast(list[dict[str, Any]], delta.get("tool_calls") or [])
            if tcs:
                fn = cast(dict[str, Any], tcs[0].get("function") or {})
                if "name" in fn:
                    tc2_names.append(str(fn["name"]))
        assert tc2_names == ["Write"]

        # Step 3: Tool result for Write arrives -> text answer
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_write",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": '{"file_path": "NOTES.md", "content": "# Notes"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_write",
                    "content": '{"diff": "..."}',
                },
            ]
        )
        s3_chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": messages,
                "tools": [
                    {"type": "function", "function": {"name": "StartTask", "parameters": {}}},
                    {"type": "function", "function": {"name": "Write", "parameters": {}}},
                ],
            },
        )
        collected_text3: list[str] = []
        for c in s3_chunks:
            choices = cast(list[dict[str, Any]], c.get("choices") or [])
            if choices:
                delta = cast(dict[str, Any], choices[0].get("delta") or {})
                if "content" in delta:
                    collected_text3.append(str(delta["content"]))
        text3 = "".join(collected_text3)
        assert "started the task and written NOTES.md" in text3

        # Step 4: PR reminder prompt arrives -> answers that no PR is needed for local project
        messages.extend(
            [
                {"role": "assistant", "content": text3},
                {
                    "role": "user",
                    "content": (
                        "Your task 'Write notes' has changes that are not in a pull request. "
                        "If the work is finished, open one with OpenPullRequest. "
                        "Otherwise say what you are waiting for."
                    ),
                },
            ]
        )
        s4_chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": messages,
                "tools": [
                    {"type": "function", "function": {"name": "StartTask", "parameters": {}}},
                    {"type": "function", "function": {"name": "Write", "parameters": {}}},
                ],
            },
        )
        collected_text4: list[str] = []
        for c in s4_chunks:
            choices = cast(list[dict[str, Any]], c.get("choices") or [])
            if choices:
                delta = cast(dict[str, Any], choices[0].get("delta") or {})
                if "content" in delta:
                    collected_text4.append(str(delta["content"]))
        text4 = "".join(collected_text4)
        assert "no pull request is needed" in text4


async def test_fake_write_ignores_pull_request_text_in_tool_results(
    fake_server: RunningFake,
) -> None:
    # A tool result that mentions a pull request is not the PR reminder: the next step runs.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Please start a task and write notes."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_start",
                    "type": "function",
                    "function": {"name": "StartTask", "arguments": '{"title": "Write notes"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_start",
            "content": "Task started. Open a pull request with OpenPullRequest when done.",
        },
    ]
    async with httpx2.AsyncClient(base_url=fake_server.base_url.removesuffix("/v1")) as client:
        chunks = await _stream_chunks(
            client,
            {
                "model": "fake-write",
                "messages": messages,
                "tools": [
                    {"type": "function", "function": {"name": "StartTask", "parameters": {}}},
                    {"type": "function", "function": {"name": "Write", "parameters": {}}},
                ],
            },
        )
    names: list[str] = []
    for c in chunks:
        choices = cast(list[dict[str, Any]], c.get("choices") or [])
        if not choices:
            continue
        delta = cast(dict[str, Any], choices[0].get("delta") or {})
        for tc in cast(list[dict[str, Any]], delta.get("tool_calls") or []):
            fn = cast(dict[str, Any], tc.get("function") or {})
            if "name" in fn:
                names.append(str(fn["name"]))
    assert names == ["Write"]
