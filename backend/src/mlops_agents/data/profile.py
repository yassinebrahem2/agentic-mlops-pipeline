"""Table profiles: per-column statistics and a small sample, computed on a locked connection."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import duckdb
from pydantic import BaseModel

from mlops_agents.connections.contract import DuckDbSource
from mlops_agents.credentials.scrub import error_text
from mlops_agents.data.constants import (
    MAX_COLUMNS,
    NEARLY_UNIQUE_SHARE,
    PROFILE_TIMEOUT_S,
    SAMPLE_ROWS,
    TEXT_CUT,
    TOP_VALUES,
)
from mlops_agents.data.duck import Deadline, DeadlineExpired, locked_connection, quote_identifier
from mlops_agents.errors import DomainError

# DESCRIBE type names that get a min and a max; DECIMAL(p,s) and TIMESTAMP* match by prefix.
_RANGE_TYPES: Final = frozenset(
    {
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "BIGNUM",
        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
        "FLOAT", "DOUBLE", "DATE",
    }
)  # fmt: skip
_RANGE_TYPE_PREFIXES: Final = ("DECIMAL", "TIMESTAMP")
_TEXT_TYPE: Final = "VARCHAR"
_NULL_CELL: Final = "NULL"
# How DuckDB's memory error starts, whichever error class a reader wraps it in.
_OUT_OF_MEMORY: Final = "Out of Memory Error"


class ColumnProfile(BaseModel):
    name: str
    type: str
    nulls: int
    distinct: int
    min: str | None
    max: str | None
    top_values: list[str]


class TableProfile(BaseModel):
    """When `timed_out` (the time limit) or `incomplete` (DuckDB's memory limit, Decision
    #432) is true, only what was computed before the limit is filled in: the other counts are
    0 and the other lists empty. `row_count` is None only when the count itself did not finish
    (Decision #430): an unknown count is never reported as 0 rows, and a finished one is
    kept."""

    row_count: int | None
    columns: list[ColumnProfile]
    more_columns: int
    sample_columns: list[str]
    sample: list[list[str]]
    timed_out: bool
    incomplete: bool
    timeout_s: float  # the limit the profile ran under


class TableUnreadable(DomainError):
    """DuckDB could not read the table (422). `folders` are the host folders the profile was
    allowed to read, which DuckDB's text may name."""

    def __init__(self, message: str, folders: Sequence[Path]) -> None:
        super().__init__("table_unreadable", message, status=422)
        self.folders = list(folders)


async def profile_table(source: DuckDbSource, timeout_s: float = PROFILE_TIMEOUT_S) -> TableProfile:
    """Profiles `source` in a worker thread; past `timeout_s`, or out of memory once the
    columns are known, it returns what it has.

    Raises `TableUnreadable` (`table_unreadable`, 422) when DuckDB cannot read the table.
    """
    return await asyncio.to_thread(_profile, source, timeout_s)


def _cut(text: str) -> str:
    return text[:TEXT_CUT]


def _cell(value: str | None) -> str:
    """A sample cell, already DuckDB's own text, cut in SQL."""
    return _NULL_CELL if value is None else value


def _has_range(type_name: str) -> bool:
    return type_name in _RANGE_TYPES or type_name.startswith(_RANGE_TYPE_PREFIXES)


def _out_of_memory(exc: duckdb.Error) -> bool:
    return isinstance(exc, duckdb.OutOfMemoryException) or str(exc).startswith(_OUT_OF_MEMORY)


def _profile(source: DuckDbSource, timeout_s: float) -> TableProfile:
    builder = _ProfileBuilder(source.sql_from)
    timed_out = incomplete = False
    connection = locked_connection(source.allowed_directories)
    try:
        deadline = Deadline(connection, timeout_s)
        try:
            builder.run(deadline)
        except DeadlineExpired:
            timed_out = True
        except duckdb.Error as exc:
            # An interrupt can surface as any DuckDB error; past the deadline it is a timeout.
            if deadline.expired:
                timed_out = True
            elif _out_of_memory(exc) and builder.described:
                # The step that ran out stops here, at every run (Decision #432).
                incomplete = True
            else:
                raise TableUnreadable(error_text(exc), source.allowed_directories) from exc
        finally:
            deadline.stop()
    finally:
        connection.close()
    return builder.result(timed_out=timed_out, incomplete=incomplete, timeout_s=timeout_s)


class _ProfileBuilder:
    """Builds every query over `(SELECT * FROM <sql_from>)` and keeps each finished result."""

    def __init__(self, sql_from: str) -> None:
        # sql_from is the connection kind's, built from a validated folder and quoted file names.
        self._relation = f"(SELECT * FROM {sql_from})"  # noqa: S608 -- DuckDB boundary; the kind quotes it
        self.described = False  # the columns' names and types are known
        self._row_count: int | None = None
        self._columns: list[ColumnProfile] = []
        self._more_columns = 0
        self._sample_columns: list[str] = []
        self._sample: list[list[str]] = []

    def run(self, deadline: Deadline) -> None:
        self._describe(deadline)
        self._aggregate(deadline)
        self._top_values(deadline)
        self._take_sample(deadline)

    def result(self, *, timed_out: bool, incomplete: bool, timeout_s: float) -> TableProfile:
        return TableProfile(
            # Set from the aggregate's one fetched row, so exact whenever it is set.
            row_count=self._row_count,
            columns=self._columns,
            more_columns=self._more_columns,
            sample_columns=self._sample_columns,
            sample=self._sample,
            timed_out=timed_out,
            incomplete=incomplete,
            timeout_s=timeout_s,
        )

    def _select(self, expressions: str, rest: str = "") -> str:
        """The one query shape: `expressions` and `rest` hold only quoted identifiers and
        this module's own SQL."""
        return f"SELECT {expressions} FROM {self._relation} {rest}"  # noqa: S608 -- DuckDB boundary; quoted names only

    def _describe(self, deadline: Deadline) -> None:
        described = deadline.fetchall(f"DESCRIBE {self._relation}")
        self._columns = [
            ColumnProfile(
                name=str(row[0]),
                type=str(row[1]),
                nulls=0,
                distinct=0,
                min=None,
                max=None,
                top_values=[],
            )
            for row in described[:MAX_COLUMNS]
        ]
        self._more_columns = len(described) - len(self._columns)
        self.described = True

    def _aggregate(self, deadline: Deadline) -> None:
        expressions = ["count(*)"]
        for column in self._columns:
            name = quote_identifier(column.name)
            expressions += [f"count(*) - count({name})", f"approx_count_distinct({name})"]
            if _has_range(column.type):
                expressions += [f"CAST(min({name}) AS VARCHAR)", f"CAST(max({name}) AS VARCHAR)"]
        (row,) = deadline.fetchall(self._select(", ".join(expressions)))
        values = iter(row)
        self._row_count = int(next(values))
        for column in self._columns:
            column.nulls = int(next(values))
            column.distinct = int(next(values))
            if _has_range(column.type):
                low, high = next(values), next(values)
                column.min = None if low is None else _cut(str(low))
                column.max = None if high is None else _cut(str(high))

    def _top_values(self, deadline: Deadline) -> None:
        for column in self._columns:
            if column.type != _TEXT_TYPE or self._nearly_unique(column):
                continue
            name = quote_identifier(column.name)
            rows = deadline.fetchall(
                self._select(
                    f"{name}, count(*)",
                    f"WHERE {name} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_VALUES}",
                )
            )
            column.top_values = [_cut(str(row[0])) for row in rows]

    def _nearly_unique(self, column: ColumnProfile) -> bool:
        """Its top values would be noise (Decision #432); the row count is set by now."""
        rows = self._row_count or 0
        return column.distinct >= NEARLY_UNIQUE_SHARE * rows

    def _take_sample(self, deadline: Deadline) -> None:
        if not self._columns:
            return
        # Cast and cut in SQL: no Python value is fetched (a TIMESTAMPTZ would need pytz,
        # infinity and nanoseconds would be lost) and a huge cell never reaches Python whole.
        cells = ", ".join(
            f"left(CAST({quote_identifier(column.name)} AS VARCHAR), {TEXT_CUT})"
            for column in self._columns
        )
        rows = deadline.fetchall(self._select(cells, f"LIMIT {SAMPLE_ROWS}"))
        self._sample_columns = [column.name for column in self._columns]
        self._sample = [[_cell(value) for value in row] for row in rows]
