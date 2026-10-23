from __future__ import annotations

from collections.abc import Sequence

import httpx2
import pytest
from langchain_core.messages import HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply, TurnFailed
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def _setup_provider_and_model(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    http: httpx2.AsyncClient,
    *,
    name: str = "fake",
    wire_model_id: str = "fake-tools",
    keys: Sequence[str] = (),
) -> tuple[ProvidersService, ResolvedModel]:
    svc = ProvidersService(databases, keystore, http)
    kind = "hosted" if keys else "self_hosted"
    prov = await svc.create_provider(ProviderCreate(name=name, kind=kind, base_url=fake.base_url))
    for k in keys:
        await svc.add_key(name, k)

    await svc.add_model(ModelCreate(provider=name, wire_model_id=wire_model_id))
    entry = await svc.verify_model(f"{name}:{wire_model_id}")
    stored_keys = tuple(await svc.keys_for(prov.id))
    assert entry.context_window is not None
    resolved = ResolvedModel(
        address=entry.address,
        provider_id=prov.id,
        provider_name=prov.name,
        base_url=prov.base_url,
        wire_model_id=entry.wire_model_id,
        context_window=entry.context_window,
        keys=stored_keys,
    )
    fake.state.requests.clear()
    fake.state.request_headers.clear()
    return svc, resolved


async def _noop(_: str) -> None:
    pass


async def test_call_with_empty_tools_sends_no_tools_field(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-no-tools"
    )
    caller = ModelCaller(svc, model_http)
    received_text: list[str] = []

    async def on_text(t: str) -> None:
        received_text.append(t)

    reply = await caller.call(model, [HumanMessage(content="hi")], tools=[], on_text=on_text)
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
    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-no-tools"
    )
    caller = ModelCaller(svc, model_http)
    received_text: list[str] = []

    async def on_text(t: str) -> None:
        received_text.append(t)

    reply = await caller.call(model, [HumanMessage(content="hi")], tools=[], on_text=on_text)
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
    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-no-tools"
    )
    caller = ModelCaller(svc, model_http)

    reply = await caller.call(model, [HumanMessage(content="hi")], tools=[], on_text=_noop)
    assert reply.prompt_tokens is None
    assert reply.completion_tokens is None


async def test_tool_call_split_across_chunks_is_merged(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-tools"
    )
    caller = ModelCaller(svc, model_http)
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

    reply = await caller.call(
        model,
        [HumanMessage(content="read readme")],
        tools=tools,
        on_text=_noop,
    )
    assert len(reply.message.tool_calls) == 1
    call = reply.message.tool_calls[0]
    assert call["name"] == "Read"
    assert call["args"] == {"file_path": "README.md"}
    call_id = call.get("id")
    assert call_id is not None and str(call_id).startswith("call_")


async def test_rate_limit_key_rotation_and_backoff(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    # 1. Two keys, rate_limit_remaining = 1 -> rotates to second key, no sleep
    svc2, model2 = await _setup_provider_and_model(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-two-keys",
        wire_model_id="fake-no-tools",
        keys=["key-one", "key-two"],
    )
    caller2 = ModelCaller(svc2, model_http, sleep=fake_sleep)
    fake_openai.state.rate_limit_remaining = 1
    fake_openai.state.requests.clear()
    fake_openai.state.request_headers.clear()

    reply = await caller2.call(
        model2,
        [HumanMessage(content="hi")],
        tools=[],
        on_text=_noop,
    )
    assert reply.message.content == "Hello from the fake model."
    assert sleeps == []
    assert len(fake_openai.state.request_headers) == 2
    assert fake_openai.state.request_headers[0]["authorization"] == "Bearer key-one"
    assert fake_openai.state.request_headers[1]["authorization"] == "Bearer key-two"

    # 2. One key, rate_limit_remaining = 1 -> one sleep of 2.0 s, then success
    sleeps.clear()
    svc1, model1 = await _setup_provider_and_model(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-one-key",
        wire_model_id="fake-no-tools",
        keys=["solo-key"],
    )
    caller1 = ModelCaller(svc1, model_http, sleep=fake_sleep)
    fake_openai.state.rate_limit_remaining = 1
    fake_openai.state.requests.clear()
    fake_openai.state.request_headers.clear()

    reply1 = await caller1.call(
        model1,
        [HumanMessage(content="hi")],
        tools=[],
        on_text=_noop,
    )
    assert reply1.message.content == "Hello from the fake model."
    assert sleeps == [2.0]

    # 3. Limit never clears -> TurnFailed after 6 attempts
    sleeps.clear()
    fake_openai.state.rate_limit_remaining = 999
    fake_openai.state.requests.clear()

    with pytest.raises(TurnFailed):
        await caller1.call(
            model1,
            [HumanMessage(content="hi")],
            tools=[],
            on_text=_noop,
        )
    assert len(fake_openai.state.requests) == 6
    assert len(sleeps) == 5  # sleeps between the 6 attempts


async def test_server_error_retries_and_failure(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-no-tools"
    )
    caller = ModelCaller(svc, model_http, sleep=fake_sleep)

    # server_error_remaining = 2 -> sleeps 1.0 and 2.0, then success
    fake_openai.state.server_error_remaining = 2
    fake_openai.state.requests.clear()
    reply = await caller.call(model, [HumanMessage(content="hi")], tools=[], on_text=_noop)
    assert reply.message.content == "Hello from the fake model."
    assert sleeps == [1.0, 2.0]

    # server_error_remaining = 3 -> sleeps 1.0 and 2.0, then TurnFailed
    sleeps.clear()
    fake_openai.state.server_error_remaining = 3
    fake_openai.state.requests.clear()
    with pytest.raises(TurnFailed):
        await caller.call(
            model,
            [HumanMessage(content="hi")],
            tools=[],
            on_text=_noop,
        )
    assert sleeps == [1.0, 2.0]


async def test_auth_failure_marks_provider_needs_attention(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    secret_key = "super-secret-key-12345"
    svc, model = await _setup_provider_and_model(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="fake-auth",
        wire_model_id="fake-no-tools",
        keys=[secret_key],
    )
    caller = ModelCaller(svc, model_http)
    fake_openai.state.auth_fail = True

    with pytest.raises(TurnFailed) as exc_info:
        await caller.call(
            model,
            [HumanMessage(content="hi")],
            tools=[],
            on_text=_noop,
        )

    err_msg = str(exc_info.value)
    assert model.provider_name in err_msg
    assert secret_key not in err_msg

    prov = await svc.get_provider_by_id(model.provider_id)
    assert prov.status == "needs_attention"
    assert prov.status_detail is not None
    assert secret_key not in prov.status_detail


async def test_error_after_streaming_started_fails_at_once_with_partial_text(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    svc, model = await _setup_provider_and_model(
        databases, keystore, fake_openai, model_http, wire_model_id="fake-no-tools"
    )
    caller = ModelCaller(svc, model_http, sleep=fake_sleep)
    fake_openai.state.drop_after_chunks = 2
    fake_openai.state.requests.clear()

    received: list[str] = []

    async def on_text(t: str) -> None:
        received.append(t)

    with pytest.raises(TurnFailed):
        await caller.call(model, [HumanMessage(content="hi")], tools=[], on_text=on_text)

    # Received partial text
    assert len(received) > 0
    # No retries occurred
    assert sleeps == []
    assert len(fake_openai.state.requests) == 1
