"""Key failover: rate limits, waiting, transient errors and what passes through."""

import asyncio
from collections.abc import Callable

import pytest

from mlops_agents.providers.failover import (
    TRANSIENT_BACKOFFS,
    AllKeysExhausted,
    KeyFailover,
    KeyState,
    RateLimited,
)
from tests.providers.fake_clock import (
    FakeClock,
    Script,
    at,
    bad_generation,
    bad_request,
    billing,
    overloaded,
    provider,
    rate_limit,
    statuses,
    unreachable,
)

COOLDOWN = 15
MODEL = "llama-3.3-70b"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def failover(clock: FakeClock) -> KeyFailover:
    return KeyFailover(clock)


async def test_a_rate_limit_moves_to_the_next_key_without_sleeping(
    clock: FakeClock, failover: KeyFailover
) -> None:
    two = provider(2)
    attempt = Script(rate_limit(), "reply")

    result = await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert result == "reply"
    assert attempt.keys == [0, 1]
    assert clock.sleeps == []
    assert failover.states(two, MODEL) == [
        KeyState(key_index=0, status="limited", since=at(0)),
        KeyState(key_index=1, status="ok", since=None),
    ]


async def test_a_non_finite_retry_after_is_absent_and_the_call_moves_to_key_2(
    clock: FakeClock, failover: KeyFailover
) -> None:
    script = Script(rate_limit("inf"), "ok")
    result = await failover.call(provider(2), MODEL, script, cooldown_minutes=COOLDOWN)
    assert result == "ok"
    assert script.keys == [0, 1]
    assert clock.sleeps == []


async def test_a_wait_round_ends_at_the_probe_time_when_it_is_the_earliest_wake(
    clock: FakeClock, failover: KeyFailover
) -> None:
    two = provider(2)
    await failover.call(two, MODEL, Script(billing(), "ok"), cooldown_minutes=1)
    clock.advance(10)
    # Key 2 is limited until 70 s (the 60 s cap); key 1 is due for a probe at 60 s.
    script = Script(rate_limit("600"), "ok")
    assert await failover.call(two, MODEL, script, cooldown_minutes=1) == "ok"
    assert clock.sleeps == [50.0]
    assert script.keys == [1, 0]


async def test_a_success_clears_the_limit_time(clock: FakeClock, failover: KeyFailover) -> None:
    one = provider(1)
    await failover.call(one, MODEL, Script(rate_limit("5"), "reply"), cooldown_minutes=COOLDOWN)
    assert statuses(failover.states(one, MODEL)) == ["ok"]

    clock.advance(120)
    attempt = Script(rate_limit("5"), "again")
    result = await failover.call(one, MODEL, attempt, cooldown_minutes=COOLDOWN)

    # Limited 125 s after the first limit, but a success came between: not a daily limit.
    assert result == "again"
    assert attempt.keys == [0, 0]
    assert clock.sleeps == [5.0, 5.0]


async def test_limited_again_after_59_9_seconds_is_not_exhausted(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    with pytest.raises(RateLimited):
        await failover.call(one, MODEL, Script(rate_limit("5")), cooldown_minutes=15, wait=False)

    clock.advance(59.9)
    with pytest.raises(RateLimited):
        await failover.call(one, MODEL, Script(rate_limit("5")), cooldown_minutes=15, wait=False)

    assert failover.states(one, MODEL) == [KeyState(key_index=0, status="limited", since=at(59.9))]


async def test_limited_again_after_60_seconds_is_exhausted_daily(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    with pytest.raises(RateLimited):
        await failover.call(one, MODEL, Script(rate_limit("5")), cooldown_minutes=15, wait=False)

    clock.advance(60.0)
    with pytest.raises(AllKeysExhausted) as raised:
        await failover.call(one, MODEL, Script(rate_limit("5")), cooldown_minutes=15, wait=False)

    assert raised.value.keys == [KeyState(key_index=0, status="exhausted_daily", since=at(60))]


async def test_a_retry_at_the_default_clear_time_finds_the_daily_limit(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    attempt = Script(rate_limit(), rate_limit())

    with pytest.raises(AllKeysExhausted):
        await failover.call(one, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert clock.sleeps == [60.0]
    assert attempt.keys == [0, 0]


@pytest.mark.parametrize(("retry_after", "waited"), [("5", 5.0), (None, 60.0), ("3600", 60.0)])
async def test_every_key_limited_waits_once_until_the_earliest_clears(
    clock: FakeClock, failover: KeyFailover, retry_after: str | None, waited: float
) -> None:
    two = provider(2)
    attempt = Script(rate_limit(), rate_limit(retry_after), "reply")

    result = await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert result == "reply"
    assert clock.sleeps == [waited]
    assert attempt.keys == [0, 1, 1 if retry_after == "5" else 0]


async def test_without_waiting_every_key_limited_raises_rate_limited(
    clock: FakeClock, failover: KeyFailover
) -> None:
    two = provider(2)
    attempt = Script(rate_limit(), rate_limit("5"))

    with pytest.raises(RateLimited) as raised:
        await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN, wait=False)

    assert raised.value.retry_after_s == 5.0
    assert clock.sleeps == []


async def test_rate_limits_spend_no_retry_budget(clock: FakeClock, failover: KeyFailover) -> None:
    two = provider(2)
    attempt = Script(*(rate_limit("1") for _ in range(10)), "reply")

    result = await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert result == "reply"
    assert attempt.keys == [0, 1] * 5 + [0]
    assert clock.sleeps == [1.0] * 5
    assert statuses(failover.states(two, MODEL)) == ["ok", "ok"]


async def test_state_is_per_model_and_per_provider(failover: KeyFailover) -> None:
    two = provider(2)
    other = provider(2, provider_id="prov_2")
    await failover.call(two, MODEL, Script(rate_limit(), "reply"), cooldown_minutes=COOLDOWN)

    assert statuses(failover.states(two, MODEL)) == ["limited", "ok"]
    assert statuses(failover.states(two, "other-model")) == ["ok", "ok"]
    assert statuses(failover.states(other, MODEL)) == ["ok", "ok"]

    attempt = Script("reply")
    await failover.call(two, "other-model", attempt, cooldown_minutes=COOLDOWN)
    assert attempt.keys == [0]


@pytest.mark.parametrize("make_error", [overloaded, unreachable])
async def test_transient_errors_retry_the_same_key_then_re_raise(
    clock: FakeClock, failover: KeyFailover, make_error: Callable[[], Exception]
) -> None:
    two = provider(2)
    last = make_error()
    attempt = Script(make_error(), make_error(), last)

    with pytest.raises(type(last)) as raised:
        await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert raised.value is last
    assert attempt.keys == [0, 0, 0]
    assert clock.sleeps == list(TRANSIENT_BACKOFFS)
    assert statuses(failover.states(two, MODEL)) == ["ok", "ok"]


async def test_a_transient_error_then_a_success_returns(
    clock: FakeClock, failover: KeyFailover
) -> None:
    attempt = Script(overloaded(), "reply")

    assert await failover.call(provider(2), MODEL, attempt, cooldown_minutes=COOLDOWN) == "reply"
    assert clock.sleeps == [TRANSIENT_BACKOFFS[0]]


@pytest.mark.parametrize(
    "error", [bad_request(), bad_generation(), RuntimeError("not a provider error")]
)
async def test_other_errors_re_raise_at_once(
    clock: FakeClock, failover: KeyFailover, error: Exception
) -> None:
    two = provider(2)
    attempt = Script(error)

    with pytest.raises(type(error)) as raised:
        await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert raised.value is error
    assert attempt.keys == [0]
    assert clock.sleeps == []


async def test_a_provider_without_keys_is_called_once_with_none(
    clock: FakeClock, failover: KeyFailover
) -> None:
    keyless = provider(0)
    assert await failover.call(keyless, MODEL, Script("reply"), cooldown_minutes=15) == "reply"

    error = rate_limit()
    attempt = Script(error)
    with pytest.raises(type(error)) as raised:
        await failover.call(keyless, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert raised.value is error
    assert attempt.keys == [None]
    assert clock.sleeps == []
    assert failover.states(keyless, MODEL) == []


async def test_a_keyless_provider_retries_a_transient_error_after_a_backoff(
    clock: FakeClock, failover: KeyFailover
) -> None:
    attempt = Script(overloaded(), "reply")

    assert await failover.call(provider(0), MODEL, attempt, cooldown_minutes=COOLDOWN) == "reply"
    assert attempt.keys == [None, None]
    assert clock.sleeps == [TRANSIENT_BACKOFFS[0]]


@pytest.mark.parametrize("make_error", [overloaded, unreachable])
async def test_a_keyless_provider_failing_every_attempt_raises_the_last_error(
    clock: FakeClock, failover: KeyFailover, make_error: Callable[[], Exception]
) -> None:
    keyless = provider(0)
    last = make_error()
    attempt = Script(*(make_error() for _ in TRANSIENT_BACKOFFS), last)

    with pytest.raises(type(last)) as raised:
        await failover.call(keyless, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert raised.value is last
    assert attempt.keys == [None] * (len(TRANSIENT_BACKOFFS) + 1)
    assert clock.sleeps == list(TRANSIENT_BACKOFFS)


async def test_two_concurrent_calls_limited_on_one_key_do_not_exhaust_it(
    clock: FakeClock, failover: KeyFailover
) -> None:
    two = provider(2)
    release = asyncio.Event()
    keys: list[int | None] = []

    async def attempt(key_index: int | None) -> str:
        keys.append(key_index)
        if key_index == 0:
            await release.wait()
            clock.advance(0.5)
            raise rate_limit()
        return "reply"

    first = asyncio.create_task(failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN))
    second = asyncio.create_task(failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == ["reply", "reply"]
    assert keys == [0, 0, 1, 1]
    assert failover.states(two, MODEL) == [
        KeyState(key_index=0, status="limited", since=at(1.0)),
        KeyState(key_index=1, status="ok", since=None),
    ]


async def test_a_cancel_during_the_wait_propagates_and_leaves_the_state_valid(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    clock.hold = asyncio.Event()
    call = asyncio.create_task(
        failover.call(one, MODEL, Script(rate_limit("5")), cooldown_minutes=COOLDOWN)
    )
    await clock.sleeping.wait()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert failover.states(one, MODEL) == [KeyState(key_index=0, status="limited", since=at(0))]
    clock.advance(5)
    attempt = Script("reply")
    assert await failover.call(one, MODEL, attempt, cooldown_minutes=COOLDOWN) == "reply"
    assert statuses(failover.states(one, MODEL)) == ["ok"]
