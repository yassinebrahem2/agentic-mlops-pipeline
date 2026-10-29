"""The day loop: time played one business date at a time, resumable after any stop.

Every calendar day is a business date (the overview states no weekend rule, and the company
counts its schedule in simulated days). A day without bookings or releases gets no file, as in
the backfill, but it is still a day done. With a company, each day also updates its checkout
and runs the jobs its schedule makes due, after the day's files. Steps (the world's work on
the company's GitHub repository) run at the start and the end of each day, and are closed on
every exit, Ctrl+C and errors included.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

from demo_world import answer_key, paths, source
from demo_world.company import Company
from demo_world.scenarios import NO_SCENARIO, Scenario

STATE_FILE = "state.json"


class SimulateError(Exception):
    """The loop cannot run with these dates or folders."""


class Step(Protocol):
    """Work the world does around each day; a step logs its own failures and never raises."""

    def start_of_day(self, day: date, origin: date) -> None:
        """Before the checkout is updated, so what a step merges takes effect that day."""

    def end_of_day(self, day: date, origin: date) -> None:
        """After the day's files and the company's jobs."""

    def close(self) -> None:
        """Undo what must not outlive the loop."""


@dataclass(frozen=True)
class World:
    """What the loop plays: the loaded source, the two folders, the scenario, the company and
    the steps around each day.

    Without a company the loop writes the day's files only.
    """

    loaded: pd.DataFrame
    data: Path
    folder: Path
    scenario: Scenario = NO_SCENARIO
    company: Company | None = None
    steps: tuple[Step, ...] = ()


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


def write_files(world: World, day: date) -> None:
    write_bookings(world, day)
    write_labels(world, day)


def _delivered_late(scenario: Scenario, day: date) -> bool:
    return any(p.kind == "score_failure" and p.active(day) for p in scenario.problems)


def play_day(world: World, day: date, origin: date) -> None:
    """One business day: its files, then the company's jobs due that day.

    A day is done only once all of it has run, so a stop midway replays the whole day; each
    file is written atomically and identically on a replay (the company's own records of a
    replayed job are appended again).
    """
    company = world.company
    if company is None:
        write_files(world, day)
        return
    # A score failure is a late upstream delivery: the day's files land after its jobs ran,
    # so the company's scoring finds no bookings file, and the history still ends up complete.
    late = _delivered_late(world.scenario, day)
    if not late:
        write_files(world, day)
    company.run_jobs(day, origin)
    if late:
        write_files(world, day)


@dataclass(frozen=True)
class _State:
    """The first day the world ever played (the schedule counts from it) and the last day done."""

    first: date
    last_done: date


def _read_state(folder: Path) -> _State | None:
    path = folder / STATE_FILE
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if "first" not in state or "last_done" not in state:
        # A world started before the schedule's first day was recorded cannot place it.
        raise SimulateError(f"{path} lacks 'first' or 'last_done'; start a new world folder")
    return _State(date.fromisoformat(state["first"]), date.fromisoformat(state["last_done"]))


def _write_state(folder: Path, state: _State) -> None:
    path = folder / STATE_FILE
    temporary = path.with_name(f"{path.name}.tmp")
    text = json.dumps({"first": state.first.isoformat(), "last_done": state.last_done.isoformat()})
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)


def _first_day(state: _State | None, start: date, end: date) -> date:
    if end < start:
        raise SimulateError(f"the end {end} is before the start {start}")
    if state is None:
        return start
    resume = state.last_done + timedelta(days=1)
    if start > resume:
        raise SimulateError(
            f"the world has played up to {state.last_done}; a start after {resume} would skip days"
        )
    return resume


def _play_days(world: World, first: date, days: int, origin: date, pace: Pace) -> None:
    next_day_at: float | None = None
    for offset in range(days):
        day = first + timedelta(days=offset)
        if next_day_at is not None and (wait := next_day_at - pace.clock()) > 0:
            pace.sleep(wait)
        began = pace.clock()
        answer_key.append_new(world.folder, world.scenario.key_records(day))
        for step in world.steps:
            step.start_of_day(day, origin)
        if world.company is not None:
            world.company.update()
            if offset == 0:
                # Before the first day's files: `train` keeps the labels released on its cutoff.
                world.company.bootstrap(cutoff=day)
        play_day(world, day, origin)
        for step in world.steps:
            step.end_of_day(day, origin)
        _write_state(world.folder, _State(origin, day))
        next_day_at = began + pace.seconds_per_day


def simulate(world: World, start: date, end: date, pace: Pace) -> Summary:
    """Play every business date from `start` (or after the last day done) to `end` included."""
    if world.folder.resolve().is_relative_to(world.data.resolve()):
        raise SimulateError(
            f"the world folder {world.folder} is inside the data folder {world.data}; "
            "the answer key would be readable by the company and the agents"
        )
    state = _read_state(world.folder)
    first = _first_day(state, start, end)
    origin = first if state is None else state.first
    world.folder.mkdir(parents=True, exist_ok=True)
    answer_key.write_planted_flaws(world.folder)

    days = (end - first).days + 1
    try:
        _play_days(world, first, days, origin, pace)
    finally:
        for step in world.steps:
            step.close()
    if days <= 0:
        return Summary(first=None, last=None, days=0)
    return Summary(first=first, last=end, days=days)
