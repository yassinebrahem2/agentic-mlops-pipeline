from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from benchmarks.fake_clock import FakeClock


def test_fake_clock_now_and_set() -> None:
    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t1)
    assert clock.now() == t1

    t2 = datetime(2026, 1, 2, 15, 30, 0, tzinfo=UTC)
    clock.set(t2)
    assert clock.now() == t2

    clock.advance(timedelta(hours=2))
    assert clock.now() == datetime(2026, 1, 2, 17, 30, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fake_clock_sleep_yields_once() -> None:
    clock = FakeClock()
    slept = False

    async def sleeper() -> None:
        nonlocal slept
        await clock.sleep(100.0)
        slept = True

    task = asyncio.create_task(sleeper())
    # sleeper starts and hits clock.sleep (which yields once with await asyncio.sleep(0))
    await asyncio.sleep(0)
    # yield once more to allow sleeper to finish after resuming from clock.sleep
    await asyncio.sleep(0)
    assert slept is True
    await task
