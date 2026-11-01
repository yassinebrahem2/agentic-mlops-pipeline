from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from benchmarks.dates import DayStarts, business_date


def test_business_date_on_day_start() -> None:
    starts: DayStarts = [
        (date(2026, 11, 1), datetime(2026, 11, 1, 9, 0, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 9, 0, tzinfo=UTC)),
        (date(2026, 11, 3), datetime(2026, 11, 3, 9, 0, tzinfo=UTC)),
    ]
    assert business_date(datetime(2026, 11, 1, 9, 0, tzinfo=UTC), starts) == date(2026, 11, 1)
    assert business_date(datetime(2026, 11, 2, 9, 0, tzinfo=UTC), starts) == date(2026, 11, 2)
    assert business_date(datetime(2026, 11, 3, 9, 0, tzinfo=UTC), starts) == date(2026, 11, 3)


def test_business_date_inside_day() -> None:
    starts: DayStarts = [
        (date(2026, 11, 1), datetime(2026, 11, 1, 9, 0, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 9, 0, tzinfo=UTC)),
    ]
    assert business_date(datetime(2026, 11, 1, 9, 1, tzinfo=UTC), starts) == date(2026, 11, 1)
    assert business_date(datetime(2026, 11, 1, 23, 59, tzinfo=UTC), starts) == date(2026, 11, 1)
    assert business_date(datetime(2026, 11, 2, 8, 59, tzinfo=UTC), starts) == date(2026, 11, 1)


def test_business_date_before_first_start() -> None:
    starts: DayStarts = [
        (date(2026, 11, 1), datetime(2026, 11, 1, 9, 0, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 9, 0, tzinfo=UTC)),
    ]
    assert business_date(datetime(2026, 11, 1, 8, 59, tzinfo=UTC), starts) == date(2026, 11, 1)
    assert business_date(datetime(2026, 10, 15, 0, 0, tzinfo=UTC), starts) == date(2026, 11, 1)


def test_business_date_after_last() -> None:
    starts: DayStarts = [
        (date(2026, 11, 1), datetime(2026, 11, 1, 9, 0, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 9, 0, tzinfo=UTC)),
    ]
    assert business_date(datetime(2026, 11, 2, 9, 1, tzinfo=UTC), starts) == date(2026, 11, 2)
    assert business_date(datetime(2026, 11, 10, 0, 0, tzinfo=UTC), starts) == date(2026, 11, 2)


def test_business_date_empty_starts() -> None:
    with pytest.raises(ValueError):
        business_date(datetime(2026, 11, 1, 9, 0, tzinfo=UTC), [])
