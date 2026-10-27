"""The day loop: time played one business date at a time, resumable after any stop.

Every calendar day is a business date (the overview states no weekend rule, and the company
counts its schedule in simulated days). A day without bookings or releases gets no file, as in
the backfill, but it is still a day done.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from demo_world import answer_key, paths, source
from demo_world.scenarios import NO_SCENARIO, Scenario

STATE_FILE = "state.json"


class SimulateError(Exception):
    """The loop cannot run with these dates or folders."""


@dataclass(frozen=True)
class World:
    """What the loop plays: the loaded source, the two folders and the scenario."""

    loaded: pd.DataFrame
    data: Path
    folder: Path
    scenario: Scenario = NO_SCENARIO


@dataclass(frozen=True)
class Pace:
    """Each day starts `seconds_per_day` after the previous one did; 0 runs flat out."""

    seconds_per_day: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Summary:
    first: date | None
    last: date | None
    days: int


def write_bookings(world: World, day: date) -> None:
    frame = source.bookings_frame(world.loaded, day)
    if frame.empty:
        return
    injected, schema = world.scenario.inject(frame, day)
    source.write_frame(injected, schema, paths.bookings_file(world.data, day))


def write_labels(world: World, day: date) -> None:
    # A label is never released before its booking date, so each of these bookings is already
    # in the data folder: from the backfill, from an earlier day, or from this day's file.
    frame = source.labels_frame(world.loaded, day)
    if not frame.empty:
        source.write_frame(frame, source.LABELS_SCHEMA, paths.labels_file(world.data, day))


type DayStep = Callable[[World, date], None]

# One business day, in order. A day is done only once every step has run, so a stop between
# steps replays the whole day; each file is written atomically and identically on a replay.
DAY_STEPS: tuple[DayStep, ...] = (write_bookings, write_labels)


def _last_done(folder: Path) -> date | None:
    path = folder / STATE_FILE
    if not path.exists():
        return None
    return date.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["last_done"])


def _mark_done(folder: Path, day: date) -> None:
    path = folder / STATE_FILE
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps({"last_done": day.isoformat()}) + "\n", encoding="utf-8")
    temporary.replace(path)


def _first_day(folder: Path, start: date, end: date) -> date:
    if end < start:
        raise SimulateError(f"the end {end} is before the start {start}")
    last = _last_done(folder)
    if last is None:
        return start
    resume = last + timedelta(days=1)
    if start > resume:
        raise SimulateError(
            f"the world has played up to {last}; a start after {resume} would skip days"
        )
    return resume


def simulate(world: World, start: date, end: date, pace: Pace) -> Summary:
    """Play every business date from `start` (or after the last day done) to `end` included."""
    if world.folder.resolve().is_relative_to(world.data.resolve()):
        raise SimulateError(
            f"the world folder {world.folder} is inside the data folder {world.data}; "
            "the answer key would be readable by the company and the agents"
        )
    first = _first_day(world.folder, start, end)
    world.folder.mkdir(parents=True, exist_ok=True)
    answer_key.write_planted_flaws(world.folder)

    days = (end - first).days + 1
    next_day_at: float | None = None
    for offset in range(days):
        day = first + timedelta(days=offset)
        if next_day_at is not None and (wait := next_day_at - pace.clock()) > 0:
            pace.sleep(wait)
        began = pace.clock()
        answer_key.append_new(world.folder, world.scenario.key_records(day))
        for step in DAY_STEPS:
            step(world, day)
        _mark_done(world.folder, day)
        next_day_at = began + pace.seconds_per_day
    if days <= 0:
        return Summary(first=None, last=None, days=0)
    return Summary(first=first, last=end, days=days)
