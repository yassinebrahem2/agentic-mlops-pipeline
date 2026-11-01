"""The scenario file: problems injected at known business dates, and natural shifts to score.

An injected problem changes the bookings frame of each day in its window after the frame is
built from the source and before it is written, and states how the written schema breaks the
contract, so `source.write_frame` keeps checking every file against the schema it is given.
`score_failure` and `ci_failure` change no file: the company step of the day loop acts on them.
`model_decay` changes no bookings file either: it changes outcomes in the source, once, before
the loop builds any day's labels from it.
"""

import hashlib
import math
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from demo_world import model_decay, source
from demo_world.answer_key import Category, Record

type Bookings = tuple[pd.DataFrame, pa.Schema]

CATEGORIES: dict[str, Category] = {
    "null_spike": "data_quality",
    "schema_break": "data_quality",
    "out_of_range": "data_quality",
    "score_failure": "operational",
    "ci_failure": "operational",
    "model_decay": "model_performance",
    "natural_shift": "drift",
}
INJECTED_KINDS = (
    "null_spike",
    "schema_break",
    "out_of_range",
    "score_failure",
    "ci_failure",
    "model_decay",
)
# The kinds that rewrite bookings files; their windows may not overlap, so no two of them
# touch one file (a null spike on a column a schema break just dropped has no meaning).
_FILE_KINDS = ("null_spike", "schema_break", "out_of_range")
_COLUMNS = source.BOOKINGS_SCHEMA.names


class ScenarioError(Exception):
    """The scenario file cannot be read or does not describe valid problems."""


@dataclass(frozen=True, kw_only=True)
class Problem:
    kind: str
    start: date
    end: date
    columns: tuple[str, ...] = ()

    def active(self, day: date) -> bool:
        return self.start <= day <= self.end

    def change(self, bookings: Bookings, day: date) -> Bookings:
        """The day's bookings as this problem leaves them; most kinds leave them as they are."""
        return bookings

    def relabel(self, loaded: pd.DataFrame, origin: date) -> pd.DataFrame:
        """The source with this problem's outcomes, in a world first played on `origin`; most
        kinds change none."""
        return loaded

    def records(self) -> list[Record]:
        columns: tuple[str | None, ...] = self.columns or (None,)
        start, end = self.start.isoformat(), self.end.isoformat()
        return [Record(self.kind, CATEGORIES[self.kind], c, start, end) for c in columns]


def _hit_rows(frame: pd.DataFrame, column: str, day: date, share: float) -> list[int]:
    # Rank rows by a digest, not `hash()` (salted per process) or a shared random state, so a
    # rerun or a restart picks the same rows; a share is rounded up so a small day is hit too.
    def rank(booking_id: int) -> bytes:
        return hashlib.sha256(f"null_spike|{column}|{day}|{booking_id}".encode()).digest()

    ids = sorted(frame["booking_id"].tolist(), key=rank)
    return ids[: math.ceil(share * len(ids))]


@dataclass(frozen=True, kw_only=True)
class NullSpike(Problem):
    share: float

    def change(self, bookings: Bookings, day: date) -> Bookings:
        frame, schema = bookings
        frame = frame.copy()
        for column in self.columns:
            hit = frame["booking_id"].isin(_hit_rows(frame, column, day, self.share))
            # Object values take a None in any column type, integer ones included.
            frame[column] = frame[column].astype(object).where(~hit, None)
            index = schema.get_field_index(column)
            schema = schema.set(index, schema.field(index).with_nullable(True))
        return frame, schema


@dataclass(frozen=True, kw_only=True)
class SchemaBreak(Problem):
    rename_to: str | None

    def change(self, bookings: Bookings, day: date) -> Bookings:
        frame, schema = bookings
        (column,) = self.columns
        index = schema.get_field_index(column)
        if self.rename_to is None:
            return frame.drop(columns=column), schema.remove(index)
        renamed = schema.set(index, schema.field(index).with_name(self.rename_to))
        return frame.rename(columns={column: self.rename_to}), renamed


@dataclass(frozen=True, kw_only=True)
class OutOfRange(Problem):
    factor: float

    def change(self, bookings: Bookings, day: date) -> Bookings:
        frame, schema = bookings
        (column,) = self.columns
        return frame.assign(**{column: frame[column] * self.factor}), schema


@dataclass(frozen=True, kw_only=True)
class ModelDecay(Problem):
    """Its window is of arrival dates: it changes the outcomes of the bookings arriving then."""

    group: tuple[tuple[str, str], ...]
    ramp_end: date
    max_share: float

    def rule(self) -> model_decay.Rule:
        return model_decay.Rule(
            group=self.group,
            start=self.start,
            ramp_end=self.ramp_end,
            end=self.end,
            max_share=self.max_share,
        )

    def relabel(self, loaded: pd.DataFrame, origin: date) -> pd.DataFrame:
        try:
            return self.rule().relabel(loaded, origin)
        except model_decay.DecayError as error:
            raise ScenarioError(f"model_decay ({self.start} to {self.end}): {error}") from error

    def records(self) -> list[Record]:
        # One line naming every column the shift lives in: the group's and the lead time.
        columns = ",".join([*(column for column, _ in self.group), "lead_time"])
        start, end = self.start.isoformat(), self.end.isoformat()
        return [Record(self.kind, CATEGORIES[self.kind], columns, start, end)]


@dataclass(frozen=True)
class Scenario:
    problems: tuple[Problem, ...]

    def inject(self, frame: pd.DataFrame, day: date) -> Bookings:
        """The day's bookings frame and the schema to write it with, once every problem is in."""
        bookings: Bookings = (frame, source.BOOKINGS_SCHEMA)
        for problem in self.problems:
            if problem.active(day):
                bookings = problem.change(bookings, day)
        return bookings

    def relabel(self, loaded: pd.DataFrame, origin: date) -> pd.DataFrame:
        """The source with every problem's outcomes, in a world first played on `origin`."""
        for problem in self.problems:
            loaded = problem.relabel(loaded, origin)
        return loaded

    def key_records(self, day: date) -> list[Record]:
        """The answer-key records of every problem whose window holds `day`."""
        return [r for p in self.problems if p.active(day) for r in p.records()]


NO_SCENARIO = Scenario(())


class _Entry:
    """One table of the file, read key by key so a missing, wrong or unexpected key is named."""

    def __init__(self, label: str, table: Mapping[str, Any]) -> None:
        self.label = label
        self._table = table
        self._read: set[str] = set()

    def error(self, message: str) -> ScenarioError:
        return ScenarioError(f"{self.label}: {message}")

    def has(self, key: str) -> bool:
        return key in self._table

    def get(self, key: str) -> Any:
        if key not in self._table:
            raise self.error(f"missing '{key}'")
        self._read.add(key)
        return self._table[key]

    def date(self, key: str) -> date:
        value = self.get(key)
        # tomllib reads a date-time as a datetime, which is also a date.
        if not isinstance(value, date) or isinstance(value, datetime):
            raise self.error(f"'{key}' must be a date (YYYY-MM-DD), not {value!r}")
        return value

    def _known(self, value: object, key: str) -> str:
        if value not in _COLUMNS:
            raise self.error(f"unknown column {value!r} in '{key}'")
        return str(value)

    def column(self, key: str) -> str:
        return self._known(self.get(key), key)

    def columns(self) -> tuple[str, ...]:
        value = self.get("columns")
        if not isinstance(value, list) or not value:
            raise self.error("'columns' must be a non-empty list of column names")
        names = tuple(self._known(v, "columns") for v in value)
        if len(set(names)) != len(names):
            raise self.error("'columns' lists a column twice")
        return names

    def number(self, key: str) -> float:
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.error(f"'{key}' must be a number, not {value!r}")
        return float(value)

    def group(self) -> tuple[tuple[str, str], ...]:
        value = self.get("group")
        if not isinstance(value, dict) or not value:
            raise self.error("'group' must be a non-empty table of text columns and their values")
        group: list[tuple[str, str]] = []
        for column, wanted in value.items():
            name = self._known(column, "group")
            if source.BOOKINGS_SCHEMA.field(name).type != pa.string() or not isinstance(
                wanted, str
            ):
                raise self.error(
                    f"'group' matches a text column to a text value, not {name} = {wanted!r}"
                )
            group.append((name, wanted))
        return tuple(group)

    def done(self) -> None:
        for key in self._table.keys() - self._read:
            raise self.error(f"unexpected '{key}'")


def _null_spike(entry: _Entry, start: date, end: date) -> Problem:
    columns, share = entry.columns(), entry.number("share")
    if not 0 < share <= 1:
        raise entry.error(f"'share' must be above 0 and at most 1, not {share}")
    return NullSpike(kind="null_spike", start=start, end=end, columns=columns, share=share)


def _schema_break(entry: _Entry, start: date, end: date) -> Problem:
    column = entry.column("column")
    if entry.has("rename_to") == entry.has("drop"):
        raise entry.error("needs exactly one of 'rename_to' and 'drop = true'")
    rename_to = None
    if entry.has("rename_to"):
        rename_to = entry.get("rename_to")
        if not isinstance(rename_to, str) or not rename_to or rename_to in _COLUMNS:
            raise entry.error(f"'rename_to' {rename_to!r} is empty or already a column")
    elif entry.get("drop") is not True:
        raise entry.error("'drop' must be true")
    return SchemaBreak(
        kind="schema_break", start=start, end=end, columns=(column,), rename_to=rename_to
    )


def _out_of_range(entry: _Entry, start: date, end: date) -> Problem:
    column, factor = entry.column("column"), entry.number("factor")
    if source.BOOKINGS_SCHEMA.field(column).type != pa.float64():
        raise entry.error(f"out_of_range multiplies a float column; {column!r} is not one")
    if factor <= 0 or factor == 1:
        raise entry.error(f"'factor' must be above 0 and not 1, not {factor}")
    return OutOfRange(kind="out_of_range", start=start, end=end, columns=(column,), factor=factor)


def _model_decay(entry: _Entry, start: date, end: date) -> Problem:
    group, max_share = entry.group(), entry.number("max_share")
    if not 0 < max_share <= 1:
        raise entry.error(f"'max_share' must be above 0 and at most 1, not {max_share}")
    ramp_end = entry.date("ramp_end")
    if not start < ramp_end <= end:
        raise entry.error(
            f"'ramp_end' must be after 'start' and at most 'end' (the share ramps from 0 at the "
            f"start to max_share at the ramp end, then holds), not {ramp_end}"
        )
    return ModelDecay(
        kind="model_decay",
        start=start,
        end=end,
        group=group,
        ramp_end=ramp_end,
        max_share=max_share,
    )


type _Builder = Callable[[_Entry, date, date], Problem]


def _failure(kind: str) -> _Builder:
    def build(entry: _Entry, start: date, end: date) -> Problem:
        return Problem(kind=kind, start=start, end=end)

    return build


_BUILDERS: dict[str, _Builder] = {
    "null_spike": _null_spike,
    "schema_break": _schema_break,
    "out_of_range": _out_of_range,
    "score_failure": _failure("score_failure"),
    "ci_failure": _failure("ci_failure"),
    "model_decay": _model_decay,
}


def _window(entry: _Entry) -> tuple[date, date]:
    start, end = entry.date("start"), entry.date("end")
    if end < start:
        raise entry.error(f"ends before it starts ({end} < {start})")
    return start, end


def _injection(number: int, table: Mapping[str, Any]) -> Problem:
    entry = _Entry(f"injection {number}", table)
    kind = entry.get("kind")
    if kind not in _BUILDERS:
        raise entry.error(f"unknown kind {kind!r}; expected one of {', '.join(INJECTED_KINDS)}")
    entry.label = f"injection {number} ({kind})"
    problem = _BUILDERS[kind](entry, *_window(entry))
    entry.done()
    return problem


def _natural_shift(number: int, table: Mapping[str, Any]) -> Problem:
    entry = _Entry(f"natural_shift {number}", table)
    columns = entry.columns()
    start, end = _window(entry)
    entry.done()
    return Problem(kind="natural_shift", start=start, end=end, columns=columns)


def _tables(document: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    tables = document.get(key, [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ScenarioError(f"'{key}' must be an array of tables ([[{key}]])")
    return tables


def _check_no_overlap(problems: list[Problem]) -> None:
    changing = [p for p in problems if p.kind in _FILE_KINDS]
    for index, first in enumerate(changing):
        for second in changing[index + 1 :]:
            if first.start <= second.end and second.start <= first.end:
                raise ScenarioError(
                    f"{first.kind} ({first.start} to {first.end}) and {second.kind} "
                    f"({second.start} to {second.end}) overlap; injections that change "
                    "bookings files must not"
                )


def _parse(document: Mapping[str, Any]) -> Scenario:
    for key in document.keys() - {"injection", "natural_shift"}:
        raise ScenarioError(f"unexpected '{key}'; expected [[injection]] and [[natural_shift]]")
    problems = [_injection(n, t) for n, t in enumerate(_tables(document, "injection"), start=1)]
    shifts = _tables(document, "natural_shift")
    problems += [_natural_shift(n, t) for n, t in enumerate(shifts, start=1)]
    _check_no_overlap(problems)
    return Scenario(tuple(problems))


def load(path: Path) -> Scenario:
    """Read and check a scenario file; every error names the file and the entry."""
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except OSError as error:
        raise ScenarioError(f"cannot read the scenario file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ScenarioError(f"scenario file {path} is not valid TOML: {error}") from error
    try:
        return _parse(document)
    except ScenarioError as error:
        raise ScenarioError(f"scenario file {path}: {error}") from None
