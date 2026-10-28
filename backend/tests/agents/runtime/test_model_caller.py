from __future__ import annotations

import httpx2
import pytest
from langchain_core.messages import HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted, ModelReply, TurnFailed
from mlops_agents.providers.failover import TRANSIENT_BACKOFFS
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import build_caller_env, keys_used
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def _noop(_: str) -> None:
    pass


async def test_call_with_empty_tools_sends_no_tools_field(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    received_text: list[str] = []

    async def on_text(t: str) -> None:
        received_text.append(t)

    reply = await env.caller.call(env.model, [HumanMessage(content="hi")], [], on_text)
    assert isinstance(reply, ModelReply)
    assert len(fake_openai.state.requests) == 1
    sent = fake_openai.state.requests[-1]
    assert "tools" not in sent


async def test_plain_reply_streams_words_and_reports_usage(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    received_text: list[str] = []

    async def on_text(t: str) -> None:
        received_text.append(t)

    reply = await env.caller.call(env.model, [HumanMessage(content="hi")], [], on_text)
    assert reply.message.content == "Hello from the fake model."
    assert reply.prompt_tokens == 100
    assert reply.completion_tokens == 10
    assert "".join(received_text) == "Hello from the fake model."
    assert len(received_text) > 1


async def test_missing_usage_in_stream_returns_none_tokens(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    fake_openai.state.usage_in_stream = False
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )

    reply = await env.caller.call(env.model, [HumanMessage(content="hi")], [], _noop)
    assert reply.prompt_tokens is None
    assert reply.completion_tokens is None


async def test_tool_call_split_across_chunks_is_merged(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(databases, keystore, fake_openai, model_http)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
        }
    ]

    reply = await env.caller.call(env.model, [HumanMessage(content="read readme")], tools, _noop)
    assert len(reply.message.tool_calls) == 1
    call = reply.message.tool_calls[0]
    assert call["name"] == "Read"
    assert call["args"] == {"file_path": "README.md"}
    call_id = call.get("id")
    assert call_id is not None and str(call_id).startswith("call_")


async def test_rate_limit_moves_to_the_next_key_then_waits_on_one_key(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    # 1. Two keys, one 429 -> the second key answers in the same call, no sleep
    two = await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-two-keys",
        wire_model_ids=["fake-no-tools"],
        keys=["key-one", "key-two"],
    )
    fake_openai.state.rate_limit_remaining = 1

    reply = await two.caller.call(two.model, [HumanMessage(content="hi")], [], _noop)
    assert reply.message.content == "Hello from the fake model."
    assert two.clock.sleeps == []
    assert keys_used(fake_openai) == ["key-one", "key-two"]

    # 2. One key, one 429 with no usable Retry-After -> one wait of the minute, then success
    one = await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-one-key",
        wire_model_ids=["fake-no-tools"],
        keys=["solo-key"],
    )
    fake_openai.state.rate_limit_remaining = 1

    reply1 = await one.caller.call(one.model, [HumanMessage(content="hi")], [], _noop)
    assert reply1.message.content == "Hello from the fake model."
    assert one.clock.sleeps == [60.0]

    # 3. The limit never clears -> limited again a minute later: daily limit, keys exhausted
    one.clock.sleeps.clear()
    fake_openai.state.rate_limit_remaining = 999
    fake_openai.state.requests.clear()

    with pytest.raises(ModelKeysExhausted):
        await one.caller.call(one.model, [HumanMessage(content="hi")], [], _noop)
    assert len(fake_openai.state.requests) == 2
    assert one.clock.sleeps == [60.0]


async def test_server_error_retries_twice_on_the_same_key_then_fails(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        wire_model_ids=["fake-no-tools"],
        keys=["key-one", "key-two"],
    )

    fake_openai.state.server_error_remaining = 2
    reply = await env.caller.call(env.model, [HumanMessage(content="hi")], [], _noop)
    assert reply.message.content == "Hello from the fake model."
    assert env.clock.sleeps == list(TRANSIENT_BACKOFFS)
    assert keys_used(fake_openai) == ["key-one"] * 3

    env.clock.sleeps.clear()
    fake_openai.state.request_headers.clear()
    fake_openai.state.server_error_remaining = 3
    with pytest.raises(TurnFailed) as failed:
        await env.caller.call(env.model, [HumanMessage(content="hi")], [], _noop)
    assert env.clock.sleeps == list(TRANSIENT_BACKOFFS)
    assert keys_used(fake_openai) == ["key-one"] * 3
    assert failed.value.error_kind is None


async def test_auth_failure_marks_provider_needs_attention(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    secret_key = "super-secret-key-12345"
    env = await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-auth",
        wire_model_ids=["fake-no-tools"],
        keys=[secret_key],
    )
    fake_openai.state.auth_fail = True

    with pytest.raises(ModelKeysExhausted) as exc_info:
        await env.caller.call(env.model, [HumanMessage(content="hi")], [], _noop)

    err_msg = str(exc_info.value)
    assert env.model.provider_name in err_msg
    assert secret_key not in err_msg

    prov = await env.providers.get_provider_by_id(env.model.provider_id)
    assert prov.status == "needs_attention"
    assert prov.status_detail is not None
    assert secret_key not in prov.status_detail


async def test_error_after_streaming_started_fails_at_once_with_partial_text(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    fake_openai.state.drop_after_chunks = 2

    received: list[str] = []

    async def on_text(t: str) -> None:
        received.append(t)

    with pytest.raises(TurnFailed) as failed:
        await env.caller.call(env.model, [HumanMessage(content="hi")], [], on_text)

    # Received partial text
    assert len(received) > 0
    # No retries occurred
    assert env.clock.sleeps == []
    assert len(fake_openai.state.requests) == 1
    # A keyless provider has no key states to add to the explanation
    assert failed.value.error_kind is None
    assert not failed.value.message.endswith(" ")


async def test_on_text_error_is_raised_as_is_and_never_fails_over(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    env = await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        wire_model_ids=["fake-no-tools"],
        keys=["key-one", "key-two"],
    )

    async def on_text(_: str) -> None:
        raise LookupError("the sink is gone")

    with pytest.raises(LookupError, match="the sink is gone"):
        await env.caller.call(env.model, [HumanMessage(content="hi")], [], on_text)
    assert keys_used(fake_openai) == ["key-one"]
    assert env.clock.sleeps == []
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    states = env.failover.states(provider, env.model.wire_model_id)
    assert [state.status for state in states] == ["ok", "ok"]
