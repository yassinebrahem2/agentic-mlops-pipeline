"""Key failover: exhausted keys, cooldown probes, Try again, the wording and the one instance."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from mlops_agents.api import services as services_module
from mlops_agents.api.services import Services
from mlops_agents.providers.failover import (
    AllKeysExhausted,
    Clock,
    KeyFailover,
    KeyState,
    KeyStatus,
    RateLimited,
)
from mlops_agents.providers.models import Provider
from tests.api.conftest import started_app
from tests.providers.fake_clock import (
    KEY_VALUES,
    FakeClock,
    Script,
    at,
    billing,
    forbidden,
    payment_required,
    provider,
    rate_limit,
    statuses,
    unauthorized,
)

COOLDOWN = 15
MODEL = "llama-3.3-70b"
MINUTE = 60.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def failover(clock: FakeClock) -> KeyFailover:
    return KeyFailover(clock)


@pytest.mark.parametrize(
    ("make_error", "status"),
    [
        (billing, "billing"),
        (payment_required, "billing"),
        (unauthorized, "rejected"),
        (forbidden, "rejected"),
    ],
)
async def test_billing_and_rejected_exhaust_at_once(
    clock: FakeClock,
    failover: KeyFailover,
    make_error: Callable[[], Exception],
    status: KeyStatus,
) -> None:
    one = provider(1)
    attempt = Script(make_error())

    with pytest.raises(AllKeysExhausted) as raised:
        await failover.call(one, MODEL, attempt, cooldown_minutes=COOLDOWN)

    assert raised.value.keys == [KeyState(key_index=0, status=status, since=at(0))]
    assert attempt.keys == [0]
    assert clock.sleeps == []


async def test_every_key_exhausted_reports_each_status_and_since(
    clock: FakeClock, failover: KeyFailover
) -> None:
    three = provider(3)
    await failover.call(three, MODEL, Script(unauthorized(), "reply"), cooldown_minutes=COOLDOWN)
    clock.advance(10)
    attempt = Script(payment_required(), rate_limit())
    with pytest.raises(RateLimited):
        await failover.call(three, MODEL, attempt, cooldown_minutes=COOLDOWN, wait=False)

    clock.advance(MINUTE)
    with pytest.raises(AllKeysExhausted) as raised:
        await failover.call(three, MODEL, Script(rate_limit()), cooldown_minutes=COOLDOWN)

    assert raised.value.keys == [
        KeyState(key_index=0, status="rejected", since=at(0)),
        KeyState(key_index=1, status="billing", since=at(10)),
        KeyState(key_index=2, status="exhausted_daily", since=at(70)),
    ]


async def test_states_reports_ok_and_limited(clock: FakeClock, failover: KeyFailover) -> None:
    two = provider(2)
    await failover.call(two, MODEL, Script(rate_limit("5"), "reply"), cooldown_minutes=COOLDOWN)

    assert failover.states(two, MODEL) == [
        KeyState(key_index=0, status="limited", since=at(0)),
        KeyState(key_index=1, status="ok", since=None),
    ]
    clock.advance(5)
    assert statuses(failover.states(two, MODEL)) == ["ok", "ok"]


async def test_exhausted_keys_are_skipped(clock: FakeClock, failover: KeyFailover) -> None:
    two = provider(2)
    await failover.call(two, MODEL, Script(unauthorized(), "reply"), cooldown_minutes=COOLDOWN)

    attempt = Script(rate_limit("5"), "again")
    assert await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN) == "again"

    assert attempt.keys == [1, 1]
    assert clock.sleeps == [5.0]


async def _exhaust_the_only_key(failover: KeyFailover) -> None:
    with pytest.raises(AllKeysExhausted):
        await failover.call(provider(1), MODEL, Script(billing()), cooldown_minutes=COOLDOWN)


async def test_after_the_cooldown_one_probe_and_a_success_clears_it(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    await _exhaust_the_only_key(failover)

    clock.advance(COOLDOWN * MINUTE - 0.1)
    early = Script()
    with pytest.raises(AllKeysExhausted):
        await failover.call(one, MODEL, early, cooldown_minutes=COOLDOWN)
    assert early.keys == []

    clock.advance(0.1)
    probe = Script("reply")
    assert await failover.call(one, MODEL, probe, cooldown_minutes=COOLDOWN) == "reply"
    assert probe.keys == [0]
    assert statuses(failover.states(one, MODEL)) == ["ok"]
    assert clock.sleeps == []


async def test_a_failed_probe_stays_exhausted_another_cooldown(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    await _exhaust_the_only_key(failover)
    clock.advance(COOLDOWN * MINUTE)

    probe = Script(rate_limit())
    with pytest.raises(AllKeysExhausted) as raised:
        await failover.call(one, MODEL, probe, cooldown_minutes=COOLDOWN)
    assert probe.keys == [0]
    assert raised.value.keys == [KeyState(key_index=0, status="billing", since=at(0))]

    clock.advance(COOLDOWN * MINUTE - 0.1)
    early = Script()
    with pytest.raises(AllKeysExhausted):
        await failover.call(one, MODEL, early, cooldown_minutes=COOLDOWN)
    assert early.keys == []

    clock.advance(0.1)
    assert await failover.call(one, MODEL, Script("reply"), cooldown_minutes=COOLDOWN) == "reply"


async def test_a_different_cooldown_changes_the_probe_time(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    await _exhaust_the_only_key(failover)
    clock.advance(5 * MINUTE)

    with pytest.raises(AllKeysExhausted):
        await failover.call(one, MODEL, Script(), cooldown_minutes=COOLDOWN)
    probe = Script("reply")
    assert await failover.call(one, MODEL, probe, cooldown_minutes=5) == "reply"
    assert probe.keys == [0]


async def test_concurrent_calls_do_not_probe_together(
    clock: FakeClock, failover: KeyFailover
) -> None:
    one = provider(1)
    await _exhaust_the_only_key(failover)
    clock.advance(COOLDOWN * MINUTE)
    release = asyncio.Event()
    keys: list[int | None] = []

    async def slow_probe(key_index: int | None) -> str:
        keys.append(key_index)
        await release.wait()
        return "reply"

    probing = asyncio.create_task(failover.call(one, MODEL, slow_probe, cooldown_minutes=COOLDOWN))
    await asyncio.sleep(0)
    other = Script("never")
    with pytest.raises(AllKeysExhausted):
        await failover.call(one, MODEL, other, cooldown_minutes=COOLDOWN)
    release.set()

    assert await probing == "reply"
    assert keys == [0]
    assert other.keys == []
    assert statuses(failover.states(one, MODEL)) == ["ok"]


async def test_clear_resets_every_key_and_model_of_the_provider(failover: KeyFailover) -> None:
    two = provider(2)
    other = provider(2, provider_id="prov_2")
    await failover.call(two, MODEL, Script(unauthorized(), "reply"), cooldown_minutes=15)
    await failover.call(two, "other-model", Script(rate_limit(), "reply"), cooldown_minutes=15)
    await failover.call(other, MODEL, Script(billing(), "reply"), cooldown_minutes=COOLDOWN)

    failover.clear(two)

    assert statuses(failover.states(two, MODEL)) == ["ok", "ok"]
    assert statuses(failover.states(two, "other-model")) == ["ok", "ok"]
    assert statuses(failover.states(other, MODEL)) == ["billing", "ok"]
    attempt = Script("reply")
    await failover.call(two, MODEL, attempt, cooldown_minutes=COOLDOWN)
    assert attempt.keys == [0]


def test_describe_keys_names_keys_by_position_never_by_value(failover: KeyFailover) -> None:
    states = [
        KeyState(key_index=0, status="exhausted_daily", since=at(0)),
        KeyState(key_index=1, status="billing", since=at(0)),
        KeyState(key_index=2, status="rejected", since=at(0)),
        KeyState(key_index=3, status="limited", since=at(120)),
        KeyState(key_index=4, status="ok", since=None),
    ]

    text = failover.describe_keys(states)

    assert text == (
        "Key 1: daily limit reached (since 14:03 UTC). "
        "Key 2: billing, out of credit. "
        "Key 3: rejected by the provider. "
        "Key 4: rate limited (since 14:05 UTC). "
        "Key 5: ok."
    )
    assert not any(value in text for value in KEY_VALUES)


async def test_all_keys_exhausted_names_no_key_value(failover: KeyFailover) -> None:
    with pytest.raises(AllKeysExhausted) as raised:
        await failover.call(provider(1), MODEL, Script(unauthorized()), cooldown_minutes=15)

    assert not any(value in str(raised.value) for value in KEY_VALUES)
    assert not any(value in repr(raised.value.keys) for value in KEY_VALUES)


async def test_build_services_builds_exactly_one_key_failover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[KeyFailover] = []

    class RecordedKeyFailover(KeyFailover):
        def __init__(
            self,
            clock: Clock | None = None,
            on_success: Callable[[Provider], Awaitable[None]] | None = None,
        ) -> None:
            super().__init__(clock, on_success)
            built.append(self)

    monkeypatch.setattr(services_module, "KeyFailover", RecordedKeyFailover)

    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert len(built) == 1
        assert services.failover is built[0]
        assert services.failover is services.failover
