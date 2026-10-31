"""The doubles of the lost-Stop races (#446): a signal once `TurnWorkers.stop` has returned, a
signal as chosen tasks start to wait for a session lock, a signal on the first of several events,
and a call held until released that records a cancel reaching it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.worker import TurnWorkers


def stop_returned(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """An event set once a call of `TurnWorkers.stop` has returned. The call is synchronous, so
    its caller goes on without yielding until its next await."""
    returned = asyncio.Event()
    original = TurnWorkers.stop

    def signalling(self: TurnWorkers, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        returned.set()
        return result

    monkeypatch.setattr(TurnWorkers, "stop", signalling)
    return returned


class LockWaits:
    """An event per watched task, set when that task first asks for a session lock: it waits
    for the lock from there on, behind the tasks that asked before it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._events: dict[asyncio.Task[Any], asyncio.Event] = {}
        original = AsyncRLock.acquire
        events = self._events

        async def acquire(lock: AsyncRLock) -> None:
            current = asyncio.current_task()
            if current is not None and current in events:
                events[current].set()
            await original(lock)

        monkeypatch.setattr(AsyncRLock, "acquire", acquire)

    def watch(self, task: asyncio.Task[Any]) -> asyncio.Event:
        """Watch a task just created, before it first runs."""
        event = asyncio.Event()
        self._events[task] = event
        return event


async def first_of(*events: asyncio.Event) -> None:
    """Until one of the events is set: the one hook each code path fires at the same point."""
    waits = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()


class CancelRecordingHold:
    """The first call of `owner.name` held until `release`, recording whether a cancel reached
    it while it was held."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        original = getattr(owner, name)

        async def held_call(*args: Any, **kwargs: Any) -> Any:
            if not self.entered.is_set():
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, held_call)
