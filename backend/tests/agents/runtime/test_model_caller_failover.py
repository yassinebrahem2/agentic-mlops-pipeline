"""The model caller on a real `KeyFailover`: the fake server answers per key, time is a
`FakeClock`, so no test waits."""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted, TurnFailed
from mlops_agents.providers.bridge import KEYLESS
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import (
    CallerEnv,
    build_caller_env,
    fail_after_text,
    keys_used,
    resolve,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import KeyOutcome, RunningFake
from tests.providers.fake_clock import payment_required, rate_limit, unauthorized

KEYS = ("gsk_first_secret_value", "gsk_second_secret_value")
HI: list[BaseMessage] = [HumanMessage(content="hi")]


async def _noop(_: str) -> None:
    pass


@pytest.fixture
async def env(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> CallerEnv:
    return await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="groq",
        wire_model_ids=["fake-no-tools", "fake-tools"],
        keys=KEYS,
    )


async def _statuses(env: CallerEnv, model: str = "fake-no-tools") -> list[str]:
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    return [state.status for state in env.failover.states(provider, model)]


async def test_a_429_on_key_one_is_answered_by_key_two_in_the_same_call(
    env: CallerEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEYS[0], [("429", None)])

    reply = await env.caller.call(env.model, HI, [], _noop)

    assert reply.message.content == "Hello from the fake model."
    assert keys_used(fake_openai) == list(KEYS)
    assert env.clock.sleeps == []


@pytest.mark.parametrize(
    "limit",
    [pytest.param(("429", 5.0), id="per-minute"), pytest.param("429-quota-wording", id="quota")],
)
async def test_a_limit_on_every_key_waits_once_then_succeeds(
    env: CallerEnv, fake_openai: RunningFake, limit: KeyOutcome
) -> None:
    for key in KEYS:
        fake_openai.state.script_for_key(key, [limit])

    reply = await env.caller.call(env.model, HI, [], _noop)

    assert reply.message.content == "Hello from the fake model."
    assert len(env.clock.sleeps) == 1
    assert keys_used(fake_openai) == [KEYS[0], KEYS[1], KEYS[0]]


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("402", "billing"), ("insufficient_quota", "billing"), ("401", "rejected")],
)
async def test_billing_and_rejected_keys_are_exhausted_at_once(
    env: CallerEnv, fake_openai: RunningFake, outcome: KeyOutcome, status: str
) -> None:
    fake_openai.state.script_for_key(KEYS[0], [outcome])

    reply = await env.caller.call(env.model, HI, [], _noop)

    assert reply.message.content == "Hello from the fake model."
    assert env.clock.sleeps == []
    assert await _statuses(env) == [status, "ok"]
    await env.caller.call(env.model, HI, [], _noop)
    assert keys_used(fake_openai) == [KEYS[0], KEYS[1], KEYS[1]]


async def test_every_key_exhausted_before_text_raises_model_keys_exhausted(
    env: CallerEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEYS[0], ["401"])
    fake_openai.state.script_for_key(KEYS[1], ["402"])

    with pytest.raises(ModelKeysExhausted) as raised:
        await env.caller.call(env.model, HI, [], _noop)

    exhausted = raised.value
    assert exhausted.error_kind == "keys_exhausted"
    assert exhausted.provider_id == env.model.provider_id
    assert exhausted.model == "fake-no-tools"
    assert [key.status for key in exhausted.keys] == ["rejected", "billing"]
    assert exhausted.description == (
        "Key 1: rejected by the provider. Key 2: billing, out of credit."
    )
    assert exhausted.headline == "groq: no usable key for fake-no-tools"
    assert exhausted.message == ("groq: no usable key for fake-no-tools. " + exhausted.description)
    for key in KEYS:
        assert key not in exhausted.message
    # No key can work until someone fixes it: the provider is flagged, as before.
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "needs_attention"
    assert provider.status_detail == exhausted.message


async def test_daily_limits_exhaust_the_keys_without_flagging_the_provider(
    env: CallerEnv, fake_openai: RunningFake
) -> None:
    for key in KEYS:
        fake_openai.state.script_for_key(key, [("429", None), ("429", None)])

    with pytest.raises(ModelKeysExhausted) as raised:
        await env.caller.call(env.model, HI, [], _noop)

    assert [key.status for key in raised.value.keys] == ["exhausted_daily", "exhausted_daily"]
    assert "Key 1: daily limit reached (since 14:04 UTC)." in raised.value.message
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "ok"


async def test_a_429_after_text_fails_the_turn_without_trying_another_key(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = fail_after_text(monkeypatch, rate_limit())
    received: list[str] = []

    async def on_text(text: str) -> None:
        received.append(text)

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], on_text)

    failed = raised.value
    assert not isinstance(failed, ModelKeysExhausted)
    assert received == ["Partial "]
    assert built == [0]
    assert failed.error_kind == "keys_exhausted"
    assert failed.message.endswith("Key 1: rate limited (since 14:03 UTC). Key 2: ok.")
    for key in KEYS:
        assert key not in failed.message
    assert env.clock.sleeps == []


async def test_parallel_calls_on_one_failover_share_its_state(
    env: CallerEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEYS[0], ["401", "401"])

    replies = await asyncio.gather(
        env.caller.call(env.model, HI, [], _noop), env.caller.call(env.model, HI, [], _noop)
    )

    assert [reply.message.content for reply in replies] == ["Hello from the fake model."] * 2
    assert await _statuses(env) == ["rejected", "ok"]
    fake_openai.state.request_headers.clear()
    await env.caller.call(env.model, HI, [], _noop)
    assert keys_used(fake_openai) == [KEYS[1]]


async def test_key_state_is_per_model(env: CallerEnv, fake_openai: RunningFake) -> None:
    fake_openai.state.script_for_key(KEYS[0], ["401"])
    await env.caller.call(env.model, HI, [], _noop)
    other = await resolve(env.providers, "groq:fake-tools")
    fake_openai.state.request_headers.clear()

    await env.caller.call(other, HI, [], _noop)

    assert keys_used(fake_openai) == [KEYS[0]]
    assert await _statuses(env, "fake-tools") == ["ok", "ok"]


async def test_reset_keys_forgets_the_exhausted_keys(
    env: CallerEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEYS[0], ["401"])
    fake_openai.state.script_for_key(KEYS[1], ["402"])
    with pytest.raises(ModelKeysExhausted):
        await env.caller.call(env.model, HI, [], _noop)

    await env.caller.reset_keys(env.model.provider_id)

    reply = await env.caller.call(env.model, HI, [], _noop)
    assert reply.message.content == "Hello from the fake model."
    assert await _statuses(env) == ["ok", "ok"]


async def test_keys_changed_during_the_call_restart_it_once(
    env: CallerEnv, fake_openai: RunningFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_keys_for = env.providers.keys_for
    reads: list[int] = []

    async def keys_for(provider_id: str) -> list[str]:
        # The first read misses the second key, as if it was added after the provider was read.
        keys = await real_keys_for(provider_id)
        reads.append(len(keys))
        return keys[:1] if len(reads) == 1 else keys

    monkeypatch.setattr(env.providers, "keys_for", keys_for)
    fake_openai.state.script_for_key(KEYS[0], ["401"])

    reply = await env.caller.call(env.model, HI, [], _noop)

    assert reply.message.content == "Hello from the fake model."
    assert len(reads) == 2
    assert keys_used(fake_openai) == list(KEYS)


async def test_keys_changed_twice_fail_the_call_unexpectedly(
    env: CallerEnv, fake_openai: RunningFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_keys_for = env.providers.keys_for

    async def keys_for(provider_id: str) -> list[str]:
        return (await real_keys_for(provider_id))[:1]

    monkeypatch.setattr(env.providers, "keys_for", keys_for)
    fake_openai.state.script_for_key(KEYS[0], ["401"])

    with pytest.raises(Exception, match="keys changed") as raised:
        await env.caller.call(env.model, HI, [], _noop)

    # Not a TurnFailed: the turn ends with the unexpected-failure text.
    assert not isinstance(raised.value, TurnFailed)


@pytest.mark.parametrize(
    ("outcome", "provider_message"),
    [
        ("401", "Invalid API key"),
        ("402", "Payment required"),
        (("429", None), "Rate limit reached"),
    ],
)
async def test_a_keyless_provider_refusing_the_call_fails_it_without_flagging_the_provider(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    outcome: KeyOutcome,
    provider_message: str,
) -> None:
    # No key to fix or rotate: the turn fails plainly and the user can send again (#384).
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    fake_openai.state.script_for_key(KEYLESS, [outcome])

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], _noop)

    assert not isinstance(raised.value, ModelKeysExhausted)
    assert raised.value.error_kind is None
    assert provider_message in raised.value.message
    assert keys_used(fake_openai) == [KEYLESS]
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "ok"


@pytest.mark.parametrize(
    ("error", "provider_message"),
    [
        (rate_limit(), "quota exceeded"),
        (payment_required(), "payment required"),
        (unauthorized(), "bad key"),
    ],
    ids=["429", "402", "401"],
)
async def test_a_keyless_provider_failing_after_text_fails_plainly(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    provider_message: str,
) -> None:
    # No key to rotate or fix, so no Try again: the provider's own message, no error kind (#384).
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    built = fail_after_text(monkeypatch, error)
    received: list[str] = []

    async def on_text(text: str) -> None:
        received.append(text)

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], on_text)

    assert received == ["Partial "]
    assert built == [0]
    assert raised.value.error_kind is None
    assert raised.value.message == provider_message
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "ok"
