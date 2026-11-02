"""Fake clock for benchmarks and replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta


class FakeClock:
    """A Clock implementation whose instant is controlled deterministically."""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            self._current = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        else:
            self._current = start if start.tzinfo is not None else start.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def set(self, dt: datetime) -> None:
        self._current = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    def advance(self, delta: timedelta | float) -> None:
        if isinstance(delta, timedelta):
            self._current += delta
        else:
            self._current += timedelta(seconds=delta)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)
