"""Business date calculation based on recorded day starts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

type DayStarts = Sequence[tuple[date, datetime]]


def business_date(at: datetime, starts: DayStarts) -> date:
    """Returns the business date for a given timestamp.

    Rules:
    - `starts` is ascending by datetime (UTC).
    - Returns the date of the last start at or before `at`.
    - If `at` is before the first start, returns the first date in `starts`.
    """
    if not starts:
        raise ValueError("DayStarts sequence cannot be empty")

    first_start = starts[0][1]
    cmp_at = at
    if cmp_at.tzinfo is None and first_start.tzinfo is not None:
        cmp_at = cmp_at.replace(tzinfo=UTC)
    elif cmp_at.tzinfo is not None and first_start.tzinfo is None:
        cmp_at = cmp_at.astimezone(UTC).replace(tzinfo=None)

    if cmp_at < first_start:
        return starts[0][0]

    for day_date, start_dt in reversed(starts):
        cmp_start = start_dt
        if cmp_start.tzinfo is None and cmp_at.tzinfo is not None:
            cmp_start = cmp_start.replace(tzinfo=UTC)
        elif cmp_start.tzinfo is not None and cmp_at.tzinfo is None:
            cmp_start = cmp_start.astimezone(UTC).replace(tzinfo=None)

        if cmp_at >= cmp_start:
            return day_date

    return starts[0][0]
