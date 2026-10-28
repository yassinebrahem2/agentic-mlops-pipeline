"""A model call whose attempt fails after text streamed: the text is taken back (`on_restart`)
before the failover goes on, so the call moves to the next key like any other failure (the
user's rule, 2026-10-28: a key running out mid-turn never ends the turn)."""

from __future__ import annotations

import httpx2
import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted, TurnFailed
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import CallerEnv, build_caller_env, script_builds
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import (
    bad_request,
    overloaded,
    payment_required,
    rate_limit,
    unauthorized,
)

KEYS = ("gsk_first_secret_value", "gsk_second_secret_value")
HI: list[BaseMessage] = [HumanMessage(content="hi")]
ANSWER = "Hello from the fake model."


class Stream:
    """What the caller told the model step, in order: each text delta and each take-back."""

    def __init__(self, restart_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self._restart_error = restart_error

    async def on_text(self, text: str) -> None:
        self.events.append(text)

    async def on_restart(self) -> None:
        self.events.append("<restart>")
        if self._restart_error is not None:
            raise self._restart_error

    @property
    def restarts(self) -> int:
        return self.events.count("<restart>")


@pytest.fixture
async def env(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> CallerEnv:
    return await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"], keys=KEYS
    )


async def _statuses(env: CallerEnv) -> list[str]:
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    return [state.status for state in env.failover.states(provider, "fake-no-tools")]


async def test_a_429_after_text_takes_the_text_back_and_the_next_key_answers(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream()

    reply = await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert reply.message.content == ANSWER
    assert built == [0, 1]
    assert stream.events[:2] == ["Partial ", "<restart>"]
    assert "".join(stream.events[2:]) == ANSWER
    assert env.clock.sleeps == []
    assert await _statuses(env) == ["limited", "ok"]


async def test_text_that_streams_to_the_end_is_never_taken_back(env: CallerEnv) -> None:
    stream = Stream()

    await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert stream.restarts == 0
    assert "".join(stream.events) == ANSWER


async def test_every_key_exhausted_after_text_takes_each_attempts_text_back(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_builds(monkeypatch, [unauthorized(), payment_required()])
    stream = Stream()

    with pytest.raises(ModelKeysExhausted) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert stream.events == ["Partial ", "<restart>", "Partial ", "<restart>"]
    assert [key.status for key in raised.value.keys] == ["rejected", "billing"]


async def test_a_transient_failure_after_text_retries_the_same_key_once_taken_back(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [overloaded()])
    stream = Stream()

    await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert built == [0, 0]
    assert stream.events[:2] == ["Partial ", "<restart>"]
    assert env.clock.sleeps == [1.0]


async def test_a_non_key_failure_after_text_fails_the_call(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model node keeps the text it was told to take back for the failed turn (graph.py).
    built = script_builds(monkeypatch, [bad_request()])
    stream = Stream()

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert raised.value.message == "bad model"
    assert built == [0]
    assert stream.events == ["Partial ", "<restart>"]
    assert await _statuses(env) == ["ok", "ok"]


async def test_a_failing_take_back_is_raised_as_itself_never_as_a_provider_error(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream(restart_error=ValueError("the hub is gone"))

    with pytest.raises(ValueError, match="the hub is gone"):
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert built == [0]
    assert await _statuses(env) == ["ok", "ok"]


async def test_without_a_take_back_a_failure_after_text_still_moves_to_the_next_key(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Compaction streams no text worth showing and passes no on_restart.
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream()

    reply = await env.caller.call(env.model, HI, [], stream.on_text)

    assert reply.message.content == ANSWER
    assert built == [0, 1]


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
    # No key to rotate or fix (Decision #384): one attempt, the provider's own message.
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    built = script_builds(monkeypatch, [error])
    stream = Stream()

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert not isinstance(raised.value, ModelKeysExhausted)
    assert raised.value.message == provider_message
    assert built == [0]
    assert stream.events == ["Partial ", "<restart>"]
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "ok"
