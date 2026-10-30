"""Tests for scheduler times and DST helpers (I24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mlops_agents.monitoring.scheduler.times import (
    in_hours,
    interval_dues,
    local_day_start,
    next_occurrences,
)
from mlops_agents.proposals.payloads import Cadence, OnCallHours


def test_interval_dues_single_and_catchup() -> None:
    # 60-second interval grid from epoch
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    t0 = epoch + timedelta(seconds=6000)  # 100 minutes
    t1 = t0 + timedelta(seconds=60)

    # 1. Regular single due: after=t0, now=t1
    dues = interval_dues(60, t0, t1)
    assert dues == [t1]

    # 2. No grid point in (after, now]
    dues_none = interval_dues(60, t0, t0 + timedelta(seconds=30))
    assert dues_none == []

    # 3. Catching up after a gap of hours: returns ONLY the latest grid point
    t_gap = t0 + timedelta(hours=5, seconds=15)
    dues_gap = interval_dues(60, t0, t_gap)
    expected_latest = epoch + timedelta(seconds=6000 + 5 * 3600)
    assert dues_gap == [expected_latest]

    # 4. after >= now returns empty
    assert interval_dues(60, t1, t0) == []
    assert interval_dues(60, t0, t0) == []


def test_next_occurrences_cron_paris_dst_march_skip() -> None:
    # March 2026: Europe/Paris jumps forward on 2026-03-29 02:00 -> 03:00.
    # 02:30 does not exist locally and must be skipped.
    cadence = Cadence(cron="30 2 * * *", every_minutes=None)
    start_utc = datetime(2026, 3, 27, 23, 0, tzinfo=UTC)  # 2026-03-28 00:00 Paris
    until_utc = datetime(2026, 3, 31, 22, 0, tzinfo=UTC)  # 2026-04-01 00:00 Paris

    occurrences = next_occurrences(cadence, "Europe/Paris", start_utc, until_utc)
    # March 28 02:30 Paris (UTC+1) = 01:30 UTC
    # March 29 02:30 skipped!
    # March 30 02:30 Paris (UTC+2) = 00:30 UTC
    # March 31 02:30 Paris (UTC+2) = 00:30 UTC
    assert occurrences == [
        datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
        datetime(2026, 3, 30, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 31, 0, 30, tzinfo=UTC),
    ]


def test_next_occurrences_cron_paris_dst_october_fires_once() -> None:
    # October 2026: Europe/Paris falls back on 2026-10-25 03:00 -> 02:00.
    # 02:30 occurs twice and must fire only ONCE (the first occurrence, fold=0, UTC+2).
    cadence = Cadence(cron="30 2 * * *", every_minutes=None)
    start_utc = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)  # 2026-10-24 00:00 Paris
    until_utc = datetime(2026, 10, 26, 23, 0, tzinfo=UTC)  # 2026-10-27 00:00 Paris

    occurrences = next_occurrences(cadence, "Europe/Paris", start_utc, until_utc)
    # Oct 24 02:30 Paris (UTC+2) = Oct 24 00:30 UTC
    # Oct 25 02:30 Paris (first occurrence, UTC+2) = Oct 25 00:30 UTC
    # Oct 26 02:30 Paris (UTC+1) = Oct 26 01:30 UTC
    assert occurrences == [
        datetime(2026, 10, 24, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 26, 1, 30, tzinfo=UTC),
    ]


def test_next_occurrences_every_minutes_across_dst() -> None:
    # every_minutes is on a UTC grid from epoch, independent of DST changes
    cadence = Cadence(cron=None, every_minutes=15)
    # October 2026 fallback weekend
    start_utc = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    until_utc = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)

    occurrences = next_occurrences(cadence, "Europe/Paris", start_utc, until_utc)
    assert len(occurrences) == 16  # 4 hours * 4 per hour
    for i in range(len(occurrences) - 1):
        delta = (occurrences[i + 1] - occurrences[i]).total_seconds()
        assert delta == 15 * 60  # exact 15 minutes in UTC


def test_in_hours_crossing_midnight() -> None:
    hours = OnCallHours(days=["fri"], start="22:00", end="06:00")
    tz = "Europe/Paris"

    # Friday 21:59 Paris -> False
    assert not in_hours(hours, tz, datetime(2026, 10, 30, 20, 59, tzinfo=UTC))
    # Friday 22:00 Paris -> True
    assert in_hours(hours, tz, datetime(2026, 10, 30, 21, 0, tzinfo=UTC))
    # Friday 23:30 Paris -> True
    assert in_hours(hours, tz, datetime(2026, 10, 30, 22, 30, tzinfo=UTC))
    # Saturday 03:00 Paris -> True (day is the start's day, Friday)
    assert in_hours(hours, tz, datetime(2026, 10, 31, 2, 0, tzinfo=UTC))
    # Saturday 06:00 Paris -> False
    assert not in_hours(hours, tz, datetime(2026, 10, 31, 5, 0, tzinfo=UTC))
    # Saturday 22:30 Paris -> False (Saturday is not in days)
    assert not in_hours(hours, tz, datetime(2026, 10, 31, 21, 30, tzinfo=UTC))
    # Thursday 23:00 Paris -> False
    assert not in_hours(hours, tz, datetime(2026, 10, 29, 22, 0, tzinfo=UTC))


def test_in_hours_same_day() -> None:
    hours = OnCallHours(days=["mon", "tue"], start="09:00", end="17:00")
    tz = "UTC"

    # Monday 08:59 UTC -> False
    assert not in_hours(hours, tz, datetime(2026, 10, 26, 8, 59, tzinfo=UTC))
    # Monday 09:00 UTC -> True
    assert in_hours(hours, tz, datetime(2026, 10, 26, 9, 0, tzinfo=UTC))
    # Monday 12:00 UTC -> True
    assert in_hours(hours, tz, datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
    # Monday 17:00 UTC -> False
    assert not in_hours(hours, tz, datetime(2026, 10, 26, 17, 0, tzinfo=UTC))
    # Wednesday 12:00 UTC -> False (Wednesday not in days)
    assert not in_hours(hours, tz, datetime(2026, 10, 28, 12, 0, tzinfo=UTC))


def test_local_day_start_non_utc() -> None:
    tz = "America/New_York"
    # 2026-10-30 02:00 UTC is 2026-10-29 22:00 EDT (UTC-4)
    at = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
    start = local_day_start(tz, at)

    # Midnight 2026-10-29 EDT is 2026-10-29 04:00 UTC
    assert start == datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
