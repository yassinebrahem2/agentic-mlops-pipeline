"""Scheduler times, intervals, cron, DST, and on-call hours (I24)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from croniter import croniter

from mlops_agents.proposals.payloads import Cadence, OnCallHours

_DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def interval_dues(interval_s: int, after: datetime, now: datetime) -> list[datetime]:
    """An interval job's dues: the latest point of a UTC grid in (after, now], or none.

    Grid points are integer multiples of interval_s from the Unix epoch (1970-01-01 00:00:00 UTC).
    On catching up, returns only the latest grid point in (after, now].
    """
    if interval_s <= 0:
        return []
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after_utc = after.astimezone(UTC)

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_utc = now.astimezone(UTC)

    if after_utc >= now_utc:
        return []

    k = math.floor(now_utc.timestamp() / interval_s)
    cand_ts = k * interval_s
    cand_dt = datetime.fromtimestamp(cand_ts, tz=UTC)

    if after_utc < cand_dt <= now_utc:
        return [cand_dt]
    return []


def next_occurrences(cadence: Cadence, tz: str, after: datetime, until: datetime) -> list[datetime]:
    """Returns UTC datetime occurrences in (after, until].

    For every_minutes: points on a UTC grid from epoch.
    For cron: evaluated in IANA zone tz using croniter.
    DST: a local time that does not exist is skipped, one that occurs twice fires once (first).
    """
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after_utc = after.astimezone(UTC)

    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    until_utc = until.astimezone(UTC)

    if after_utc >= until_utc:
        return []

    if cadence.every_minutes is not None:
        step_s = cadence.every_minutes * 60
        k_start = math.floor(after_utc.timestamp() / step_s) + 1
        k_end = math.floor(until_utc.timestamp() / step_s)
        results: list[datetime] = []
        for k in range(k_start, k_end + 1):
            dt = datetime.fromtimestamp(k * step_s, tz=UTC)
            if after_utc < dt <= until_utc:
                results.append(dt)
        return results

    if cadence.cron is not None:
        tz_info = ZoneInfo(tz)
        start_local = after_utc.astimezone(tz_info).replace(tzinfo=None)
        it = croniter(cadence.cron, start_local)
        results = []
        max_iterations = 100_000
        for _ in range(max_iterations):
            cand_naive = it.get_next(datetime)
            # fold=0 ensures the first occurrence is selected when a local time occurs twice
            cand_aware = cand_naive.replace(tzinfo=tz_info, fold=0)
            # Verify the local time exists in the timezone (detect spring-forward gap)
            back = cand_aware.astimezone(UTC).astimezone(tz_info)
            if (
                back.year != cand_naive.year
                or back.month != cand_naive.month
                or back.day != cand_naive.day
                or back.hour != cand_naive.hour
                or back.minute != cand_naive.minute
                or back.second != cand_naive.second
            ):
                continue
            cand_utc = cand_aware.astimezone(UTC)
            if cand_utc > until_utc:
                break
            if cand_utc > after_utc:
                results.append(cand_utc)
        return results

    return []


def in_hours(hours: OnCallHours, tz: str, at: datetime) -> bool:
    """Checks whether the UTC instant at converted to timezone tz falls within hours."""
    tz_info = ZoneInfo(tz)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    local_dt = at.astimezone(tz_info)

    current_day = _DAY_NAMES[local_dt.weekday()]
    prev_day = _DAY_NAMES[(local_dt.weekday() - 1) % 7]

    start_h, start_m = map(int, hours.start.split(":"))
    end_h, end_m = map(int, hours.end.split(":"))
    start_t = time(start_h, start_m)
    end_t = time(end_h, end_m)
    local_t = local_dt.time()

    if start_t < end_t:
        return current_day in hours.days and (start_t <= local_t < end_t)
    else:  # end_t < start_t (crosses midnight)
        if current_day in hours.days and local_t >= start_t:
            return True
        return bool(prev_day in hours.days and local_t < end_t)


def local_day_start(tz: str, at: datetime) -> datetime:
    """Start of day (00:00:00) in timezone tz for the day of at, converted to aware UTC."""
    tz_info = ZoneInfo(tz)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    local_dt = at.astimezone(tz_info)
    local_date = local_dt.date()
    midnight_naive = datetime.combine(local_date, time(0, 0, 0))
    midnight_aware = midnight_naive.replace(tzinfo=tz_info, fold=0)
    return midnight_aware.astimezone(UTC)
