from __future__ import annotations

from typing import Any, cast

import httpx2
import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage

from mlops_agents.providers.bridge import SKIP_THOUGHT_SIGNATURE_VALIDATOR, build_chat_model
from tests.fakes.fake_openai import RunningFake

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
]


def _merge_chunks(chunks: list[AIMessageChunk]) -> AIMessageChunk:
    total = chunks[0]
    for c in chunks[1:]:
        total = total + c
    return total


@pytest.mark.asyncio
async def test_streamed_tool_call_extra_content_sent_back_on_next_request(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "README.md"},
            "extra_content": {"google": {"thought_signature": "sig_abc"}},
        }
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read the readme")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tool_call_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read the readme"),
        total_chunk,
        ToolMessage(tool_call_id=tool_call_id, content="File contents here"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    assert tc.get("extra_content") == {"google": {"thought_signature": "sig_abc"}}


@pytest.mark.asyncio
async def test_two_tool_calls_only_one_with_extra_content(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
            "extra_content": {"google": {"thought_signature": "sig_0"}},
        },
        {
            "name": "Read",
            "arguments": {"file_path": "b.txt"},
        },
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read both")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc0_id = str(tool_calls[0]["id"])
    tc1_id = str(tool_calls[1]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read both"),
        total_chunk,
        ToolMessage(tool_call_id=tc0_id, content="Content A"),
        ToolMessage(tool_call_id=tc1_id, content="Content B"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    assert len(tcs) == 2
    assert tcs[0].get("extra_content") == {"google": {"thought_signature": "sig_0"}}
    assert "extra_content" not in tcs[1]


@pytest.mark.asyncio
async def test_same_value_arriving_on_two_deltas_comes_back_exactly_once(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
            "extra_content": {"google": {"thought_signature": "sig_abc"}},
            "repeat_extra_content_on_second_delta": True,
            "repeat_id_on_second_delta": True,
        }
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Content A"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    assert tc.get("extra_content") == {"google": {"thought_signature": "sig_abc"}}


@pytest.mark.asyncio
async def test_extra_content_containing_repeat_comes_back_unchanged(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
            "extra_content": {"google": {"thought_signature": "AAAA"}},
        }
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Content A"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    # Opaque repeats such as "AAAA" must never be collapsed to "A"
    assert tc.get("extra_content") == {"google": {"thought_signature": "AAAA"}}


@pytest.mark.asyncio
async def test_provider_sending_no_extra_content_unchanged_request(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
        }
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Content A"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    assert "extra_content" not in tc


@pytest.mark.asyncio
async def test_history_from_one_model_sent_to_another_drops_extra_content(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
            "extra_content": {"google": {"thought_signature": "sig_abc"}},
        }
    ]

    client1 = build_chat_model(
        fake_openai.base_url,
        "test-key",
        "fake-tools",
        http=model_http,
        model_address="prov/model-1",
    )
    runnable1: Any = client1.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable1.astream([HumanMessage(content="Read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    # Send follow-up to client2 with different model address
    client2 = build_chat_model(
        fake_openai.base_url,
        "test-key",
        "fake-write",
        http=model_http,
        model_address="prov/model-2",
    )
    runnable2: Any = client2.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Content A"),
    ]
    async for _ in runnable2.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    assert "extra_content" not in tc


@pytest.mark.asyncio
async def test_two_tool_calls_in_one_reply_both_with_extra_content(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    # Both tool calls repeat id and extra_content on their second deltas;
    # this verifies neither call's extra_content is doubled and fails if the fix is reverted.
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "a.txt"},
            "extra_content": {"google": {"thought_signature": "sig_0"}},
            "repeat_extra_content_on_second_delta": True,
            "repeat_id_on_second_delta": True,
        },
        {
            "name": "Read",
            "arguments": {"file_path": "b.txt"},
            "extra_content": {"google": {"thought_signature": "sig_1"}},
            "repeat_extra_content_on_second_delta": True,
            "repeat_id_on_second_delta": True,
        },
    ]

    client = build_chat_model(fake_openai.base_url, "test-key", "fake-tools", http=model_http)
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Read both")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    assert len(tool_calls) == 2

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Read both"),
        total_chunk,
        ToolMessage(tool_call_id=str(tool_calls[0]["id"]), content="Content 0"),
        ToolMessage(tool_call_id=str(tool_calls[1]["id"]), content="Content 1"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    assert len(tcs) == 2
    assert tcs[0].get("extra_content") == {"google": {"thought_signature": "sig_0"}}
    assert tcs[1].get("extra_content") == {"google": {"thought_signature": "sig_1"}}


@pytest.mark.asyncio
async def test_unsigned_history_sent_to_gemini3_receives_skip_thought_signature_validator(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    # Older history from another session/model has no extra_content
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "old.txt"},
        }
    ]

    client = build_chat_model(
        fake_openai.base_url,
        "test-key",
        "fake-tools",
        http=model_http,
        model_address="gemini/gemini-2.5-pro",
        is_gemini=True,
    )
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Old read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    # Sending follow up to Gemini 3 model (fake-tools) with unsigned history
    follow_up: list[BaseMessage] = [
        HumanMessage(content="Old read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Old content"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    # Documented fallback placeholder is sent to Gemini model for unsigned history
    assert tc.get("extra_content") == {
        "google": {"thought_signature": SKIP_THOUGHT_SIGNATURE_VALIDATOR}
    }


@pytest.mark.asyncio
async def test_unsigned_history_sent_to_other_provider_has_no_extra_content(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    # Older history without signatures
    fake_openai.state.scripted_tool_calls = [
        {
            "name": "Read",
            "arguments": {"file_path": "old.txt"},
        }
    ]

    # Non-Gemini model (e.g. OpenAI GPT-4o)
    client = build_chat_model(
        fake_openai.base_url,
        "test-key",
        "fake-tools",
        http=model_http,
        model_address="openai/gpt-4o",
        is_gemini=False,
    )
    runnable: Any = client.bind_tools(TOOLS)  # pyright: ignore[reportUnknownMemberType]

    chunks: list[AIMessageChunk] = []
    async for chunk in runnable.astream([HumanMessage(content="Old read")]):
        chunks.append(cast(AIMessageChunk, chunk))

    total_chunk = _merge_chunks(chunks)
    tool_calls = cast(list[dict[str, Any]], total_chunk.tool_calls)
    tc_id = str(tool_calls[0]["id"])

    fake_openai.state.requests.clear()
    fake_openai.state.scripted_tool_calls = None

    follow_up: list[BaseMessage] = [
        HumanMessage(content="Old read"),
        total_chunk,
        ToolMessage(tool_call_id=tc_id, content="Old content"),
    ]
    async for _ in runnable.astream(follow_up):
        pass

    assert len(fake_openai.state.requests) >= 1
    sent_request = fake_openai.state.requests[-1]
    msgs = cast(list[dict[str, Any]], sent_request["messages"])
    assistant_msg = next(m for m in msgs if m.get("role") == "assistant")
    tcs = cast(list[dict[str, Any]], assistant_msg["tool_calls"])
    tc = tcs[0]
    # No other provider gets thought signatures or the fallback placeholder
    assert "extra_content" not in tc
