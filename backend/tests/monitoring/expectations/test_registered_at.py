"""An expectation never judges a slot from a time it was not active (bug #586, task #589)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import Cadence, HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.monitoring.expectations.test_service import FakeIncidents
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.proposals.samples import data_feed, handoff
from tests.providers.fake_clock import FakeClock

PROJECT = "prj_registered"
# Registration at 12:20: the old look-back (now - grace - check grace = 11:35) holds the 12:00
# first deadline and the 11:00 second deadline, both slots from before the expectation existed.
REGISTERED = datetime(2026, 10, 31, 12, 20, tzinfo=UTC)
HOURLY = Cadence(cron=None, every_minutes=60)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=REGISTERED)


def _service(
    databases: Databases,
    clock: FakeClock,
    incidents: FakeIncidents,
    handoffs: FakeHandoffs | None = None,
) -> ExpectationsService:
    return ExpectationsService(
        databases=databases,
        handoffs=handoffs or FakeHandoffs(),
        connections=FakeConnections(),
        incidents=incidents,
        clock=clock,
    )


def _hourly_feed(ref: str = "feed_hourly", activates_on_pr: int | None = None) -> ExpectationSpec:
    return ExpectationSpec(
        key=ExpectationKey(kind="feed", ref=ref),
        category="data_quality",
        cadence=HOURLY,
        timezone="UTC",
        grace_minutes=15,
        second_deadline=True,
        activates_on_pr=activates_on_pr,
        source="handoff",
    )


async def test_a_new_expectation_raises_nothing_for_a_slot_before_it_existed(
    databases: Databases, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    service = _service(databases, clock, incidents)
    await service.register(PROJECT, _hourly_feed())

    assert await service.evaluate(PROJECT, clock.now()) == []
    assert incidents.signals == []


async def test_a_restart_still_raises_nothing_for_a_slot_before_registration(
    databases: Databases, clock: FakeClock
) -> None:
    await _service(databases, clock, FakeIncidents()).register(PROJECT, _hourly_feed())

    clock.advance(60)
    incidents = FakeIncidents()
    restarted = _service(databases, clock, incidents)
    assert await restarted.evaluate(PROJECT, clock.now()) == []
    assert incidents.signals == []


async def test_a_slot_missed_after_registration_still_raises_silence(
    databases: Databases, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    service = _service(databases, clock, incidents)
    await service.register(PROJECT, _hourly_feed())

    # 13:00 is the first slot after registration; its first deadline is 13:15.
    clock.current = datetime(2026, 10, 31, 13, 20, tzinfo=UTC)
    signals = await service.evaluate(PROJECT, clock.now())

    assert len(signals) == 1
    [(_, draft, _)] = incidents.signals
    assert draft.kind == "silence"
    assert draft.detail["occurrence"] == "2026-10-31T13:00:00+00:00"


async def test_a_due_at_before_registration_is_not_judged(
    databases: Databases, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    service = _service(databases, clock, incidents)
    spec = ExpectationSpec(
        key=ExpectationKey(kind="training_request", ref="trq_past"),
        category="operational",
        due_at=REGISTERED - timedelta(hours=1),
        timezone="UTC",
        grace_minutes=15,
        second_deadline=False,
        source="training",
    )
    await service.register(PROJECT, spec)

    assert await service.evaluate(PROJECT, clock.now()) == []
    assert incidents.signals == []


async def _registered_at(service: ExpectationsService, key: ExpectationKey) -> datetime:
    record = await service.store.get(PROJECT, key)
    assert record is not None
    return record[5]


def _payload(*feeds: dict[str, object]) -> HandoffPayload:
    raw = handoff()
    raw["operations"]["feeds"] = list(feeds)
    raw["operations"]["checks"] = []
    return HandoffPayload.model_validate(raw)


async def test_a_reseed_and_a_reconcile_of_an_active_key_keep_registered_at(
    databases: Databases, clock: FakeClock
) -> None:
    handoffs = FakeHandoffs()
    service = _service(databases, clock, FakeIncidents(), handoffs)
    feed = data_feed(name="feed_bookings", cadence={"cron": None, "every_minutes": 60})
    key = ExpectationKey(kind="feed", ref="feed_bookings")
    payload = _payload(feed)

    await service.seed(PROJECT, payload)
    assert await _registered_at(service, key) == REGISTERED

    # A second handoff with the same feed, under new settings.
    clock.advance(3600)
    await service.seed(PROJECT, _payload({**feed, "grace_minutes": 5}))
    assert await _registered_at(service, key) == REGISTERED

    # The startup reconcile from the current handoff.
    clock.advance(3600)
    handoffs.set_handoff(
        HandoffView(proposal_id="p1", version=1, approved=clock.now(), payload=payload)
    )
    await service.reconcile(PROJECT)
    assert await _registered_at(service, key) == REGISTERED


async def test_a_retire_then_register_resets_registered_at_to_the_register_time(
    databases: Databases, clock: FakeClock
) -> None:
    service = _service(databases, clock, FakeIncidents())
    feed = data_feed(name="feed_bookings", cadence={"cron": None, "every_minutes": 60})
    key = ExpectationKey(kind="feed", ref="feed_bookings")
    payload = _payload(feed)
    await service.seed(PROJECT, payload)

    # A handoff without the feed retires it; the next one with it registers it again.
    clock.advance(3600)
    await service.seed(PROJECT, _payload())
    record = await service.store.get(PROJECT, key)
    assert record is not None and record[1] is False
    clock.advance(3600)
    await service.seed(PROJECT, payload)
    record = await service.store.get(PROJECT, key)
    assert record is not None and record[1] is True
    assert record[5] == clock.now()

    # A retire and register through the service resets it too.
    clock.advance(3600)
    await service.retire(PROJECT, key)
    clock.advance(3600)
    await service.register(PROJECT, record[0])
    assert await _registered_at(service, key) == clock.now()


async def test_an_active_key_registered_inactive_keeps_registered_at_until_activated(
    databases: Databases, clock: FakeClock
) -> None:
    service = _service(databases, clock, FakeIncidents())
    spec = _hourly_feed(activates_on_pr=42)
    await service.register(PROJECT, spec, active=True)

    clock.advance(3600)
    await service.register(PROJECT, spec, active=False)
    record = await service.store.get(PROJECT, spec.key)
    assert record is not None and record[1] is False
    assert record[5] == REGISTERED

    clock.advance(3600)
    assert await service.activate_for_pr(PROJECT, 42) == [spec.key]
    assert await _registered_at(service, spec.key) == clock.now()


async def test_a_retired_then_re_added_key_raises_nothing_for_the_retired_slots(
    databases: Databases, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    service = _service(databases, clock, incidents)
    feed = data_feed(
        name="feed_bookings",
        cadence={"cron": None, "every_minutes": 60},
        grace_minutes=15,
        timezone="UTC",
    )
    key = ExpectationKey(kind="feed", ref="feed_bookings")
    payload = _payload(feed)
    await service.seed(PROJECT, payload)
    clock.current = datetime(2026, 10, 31, 13, 10, tzinfo=UTC)
    await service.note_seen(PROJECT, key, datetime(2026, 10, 31, 13, 5, tzinfo=UTC))

    # Retired at 13:30; the 14:00 to 17:00 slots pass while it is retired.
    clock.current = datetime(2026, 10, 31, 13, 30, tzinfo=UTC)
    await service.seed(PROJECT, _payload())
    clock.current = datetime(2026, 10, 31, 17, 30, tzinfo=UTC)
    await service.seed(PROJECT, payload)

    assert await service.evaluate(PROJECT, clock.now()) == []
    assert incidents.signals == []

    # 18:00 is the first slot after the re-add; its first deadline is 18:15.
    clock.current = datetime(2026, 10, 31, 18, 20, tzinfo=UTC)
    signals = await service.evaluate(PROJECT, clock.now())

    assert len(signals) == 1
    [(_, draft, _)] = incidents.signals
    assert draft.kind == "silence"
    assert draft.detail["occurrence"] == "2026-10-31T18:00:00+00:00"


async def test_an_inactive_expectation_is_not_judged_before_its_activation(
    databases: Databases, clock: FakeClock
) -> None:
    incidents = FakeIncidents()
    service = _service(databases, clock, incidents)
    spec = _hourly_feed(activates_on_pr=42)
    await service.register(PROJECT, spec)

    # Activated at 13:20: the look-back (13:20 - grace - check grace = 12:35) holds the 13:00
    # slot, which passed while the expectation was inactive.
    clock.advance(3600)
    assert await service.activate_for_pr(PROJECT, 42) == [spec.key]
    assert await service.evaluate(PROJECT, clock.now()) == []
    assert incidents.signals == []

    # 14:00 is the first slot after the activation; its first deadline is 14:15.
    clock.advance(3600)
    signals = await service.evaluate(PROJECT, clock.now())

    assert len(signals) == 1
    [(_, draft, _)] = incidents.signals
    assert draft.kind == "silence"
    assert draft.detail["occurrence"] == "2026-10-31T14:00:00+00:00"
