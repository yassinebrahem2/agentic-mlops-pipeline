"""A model call whose attempt fails after text streamed: the call moves to the next key like any
other failure, and the next attempt takes the text back (`on_restart`) when it starts (the
user's rule, 2026-10-28, and Decision #410). A failure with no next attempt keeps its text."""

from __future__ import annotations

import httpx2
import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted, TurnFailed
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import (
    CallerEnv,
    ScriptedClient,
    build_caller_env,
    script_builds,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import (
    FakeClock,
    bad_request,
    overloaded,
    payment_required,
    rate_limit,
    unauthorized,
)

KEYS = ("gsk_first_secret_value", "gsk_second_secret_value")
HI: list[BaseMessage] = [HumanMessage(content="hi")]
ANSWER = "Hello from the fake model."
RESTART = "<restart>"


class Stream:
    """What the caller told the model step, in order: each text delta and each take-back, and
    how many sleeps the clock had recorded at each take-back."""

    def __init__(self, clock: FakeClock, restart_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.sleeps_at_restart: list[int] = []
        self._clock = clock
        self._restart_error = restart_error

    async def on_text(self, text: str) -> None:
        self.events.append(text)

    async def on_restart(self) -> None:
        self.events.append(RESTART)
        self.sleeps_at_restart.append(len(self._clock.sleeps))
        if self._restart_error is not None:
            raise self._restart_error

    @property
    def restarts(self) -> int:
        return self.events.count(RESTART)


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


async def test_a_429_after_text_is_taken_back_when_the_next_key_starts(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream(env.clock)

    reply = await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert reply.message.content == ANSWER
    assert built == [0, 1]
    assert stream.events[:2] == ["Partial ", RESTART]
    assert "".join(stream.events[2:]) == ANSWER
    assert env.clock.sleeps == []
    assert await _statuses(env) == ["limited", "ok"]


async def test_text_that_streams_to_the_end_is_never_taken_back(env: CallerEnv) -> None:
    stream = Stream(env.clock)

    await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert stream.restarts == 0
    assert "".join(stream.events) == ANSWER


async def test_every_key_exhausted_after_text_leaves_the_last_attempts_text(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No attempt follows the last one: the model node takes its text back before it stalls.
    script_builds(monkeypatch, [unauthorized(), payment_required()])
    stream = Stream(env.clock)

    with pytest.raises(ModelKeysExhausted) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert stream.events == ["Partial ", RESTART, "Partial "]
    assert [key.status for key in raised.value.keys] == ["rejected", "billing"]


async def test_a_transient_failure_after_text_takes_back_once_when_the_retry_starts(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [overloaded()])
    stream = Stream(env.clock)

    await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert built == [0, 0]
    assert stream.events[:2] == ["Partial ", RESTART]
    assert stream.restarts == 1
    # After the backoff, at the start of the retry, not when the attempt failed.
    assert env.clock.sleeps == [1.0]
    assert stream.sleeps_at_restart == [1]


async def test_a_non_key_failure_after_text_takes_nothing_back(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [bad_request()])
    stream = Stream(env.clock)

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert raised.value.message == "bad model"
    assert built == [0]
    assert stream.events == ["Partial "]
    assert await _statuses(env) == ["ok", "ok"]


async def test_text_taken_back_stays_gone_when_the_next_key_fails_before_text(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [rate_limit(), ScriptedClient([], bad_request())])
    stream = Stream(env.clock)

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert raised.value.message == "bad model"
    assert built == [0, 1]
    assert stream.events == ["Partial ", RESTART]


async def test_a_failing_take_back_is_raised_as_itself_never_as_a_provider_error(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream(env.clock, restart_error=ValueError("the hub is gone"))

    with pytest.raises(ValueError, match="the hub is gone"):
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    # Key 0's limit is recorded; the failing take-back starts key 1's attempt and ends it.
    assert built == [0]
    assert await _statuses(env) == ["limited", "ok"]


async def test_without_a_take_back_a_failure_after_text_still_moves_to_the_next_key(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Compaction streams no text worth showing and passes no on_restart.
    built = script_builds(monkeypatch, [rate_limit()])
    stream = Stream(env.clock)

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
    # No key to rotate or fix (Decision #384): one attempt, the provider's own message, and no
    # attempt after it to take the text back.
    env = await build_caller_env(
        databases, keystore, fake_openai, model_http, wire_model_ids=["fake-no-tools"]
    )
    built = script_builds(monkeypatch, [error])
    stream = Stream(env.clock)

    with pytest.raises(TurnFailed) as raised:
        await env.caller.call(env.model, HI, [], stream.on_text, stream.on_restart)

    assert not isinstance(raised.value, ModelKeysExhausted)
    assert raised.value.message == provider_message
    assert built == [0]
    assert stream.events == ["Partial "]
    provider = await env.providers.get_provider_by_id(env.model.provider_id)
    assert provider.status == "ok"
