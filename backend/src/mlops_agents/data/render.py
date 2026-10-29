"""The data summaries as text: a table's profile, and the lines of the `data_summary` hook.

Names, file names, values and error reasons come from the team's files: each goes through
`quoted` or `one_line`, so it stays on its own line and a line of it cannot pass for one of
ours. The hook registry escapes data tags in the whole text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Final

from mlops_agents.connections.contract import ConnectionView, TableRef
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.data.constants import LISTED_FILES, LISTED_TABLE_NAMES
from mlops_agents.data.profile import ColumnProfile, TableProfile

SIZE_CAP: Final = "size cap"
TIME_BUDGET: Final = "time budget"
CACHE_UNAVAILABLE: Final = "profile cache unavailable"
UNKNOWN_ROW_COUNT: Final = "row count unknown (timed out)"

# What JSON leaves unescaped without `ensure_ascii`: DEL, the C1 controls (NEL among them),
# the Unicode line and paragraph separators, and lone surrogates (a file name whose bytes
# are not UTF-8), which would make the text fail to encode as UTF-8.
_UNESCAPED: Final = re.compile("[\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\ud800-\udfff]")


def _escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


def _json(value: str | Sequence[str]) -> str:
    text = json.dumps(value if isinstance(value, str) else list(value), ensure_ascii=False)
    return _UNESCAPED.sub(_escape, text)


def quoted(text: str) -> str:
    """`text` as a one-line JSON string."""
    return _json(text)


def one_line(text: str) -> str:
    """`text` escaped as in `quoted`, without the quotes."""
    return quoted(text)[1:-1]


def header() -> str:
    return f"Data connections (read-only, mounted in the sandbox at {DATA_MOUNT_ROOT}/<connection name>):"


def connection_line(view: ConnectionView) -> str:
    return (
        f"Connection {quoted(view.name)} ({one_line(view.kind)}, {view.status}) at "
        f"{DATA_MOUNT_ROOT}/{one_line(view.name)}:"
    )


def unreachable_line(view: ConnectionView, detail: str | None) -> str:
    return (
        f"Connection {quoted(view.name)} ({one_line(view.kind)}, unreachable): "
        f"{one_line(detail or '')}"
    )


def no_tables_line() -> str:
    return "No tables."


def left_out_line(ref: TableRef, reason: str) -> str:
    return f"Table {quoted(ref.name)} ({ref.format}): detail left out: {reason}"


def unreadable_line(ref: TableRef, reason: str) -> str:
    return f"Table {quoted(ref.name)} ({ref.format}): unreadable: {one_line(reason)}"


def more_tables_line(refs: Sequence[TableRef]) -> str:
    names = ", ".join(quoted(ref.name) for ref in refs[:LISTED_TABLE_NAMES])
    line = f"and {len(refs)} more tables: {names}"
    unnamed = len(refs) - LISTED_TABLE_NAMES
    if unnamed > 0:
        line += f", and {unnamed} more"
    return line


def render_table(ref: TableRef, profile: TableProfile) -> str:
    """The table's profile as text, as the Data page shows it."""
    return _table(ref, profile, data_version=None)


def render_versioned_table(ref: TableRef, data_version: str, profile: TableProfile) -> str:
    """`render_table` with the data version the profile was computed for in its header."""
    return _table(ref, profile, data_version=data_version)


def _table(ref: TableRef, profile: TableProfile, data_version: str | None) -> str:
    about = f"{ref.format}, files {_files(ref.files)}"
    if data_version is not None:
        about += f", data version {one_line(data_version)}"
    rows = UNKNOWN_ROW_COUNT if profile.row_count is None else f"{profile.row_count} rows"
    title = f"Table {quoted(ref.name)} ({about}): {rows}"
    if profile.timed_out:
        # Only DESCRIBE's names and types are known; the counts are not facts.
        lines = [
            title,
            f"Profile timed out after {profile.timeout_s:g} s: the statistics and sample rows "
            "are unknown.",
            *_columns(profile, lambda c: f"- {quoted(c.name)} {one_line(c.type)}"),
        ]
        return "\n".join(lines)
    lines = [title, *_columns(profile, _column_line)]
    if profile.sample:
        lines.append(f"Sample rows (first {len(profile.sample)}):")
        lines += [_json(row) for row in profile.sample]
    else:
        lines.append("Sample rows: none")
    return "\n".join(lines)


def _files(files: Sequence[str]) -> str:
    text = _json(files[:LISTED_FILES])
    unlisted = len(files) - LISTED_FILES
    if unlisted > 0:
        text += f" and {unlisted} more files"
    return text


def _columns(profile: TableProfile, line: Callable[[ColumnProfile], str]) -> list[str]:
    if not profile.columns:
        return []
    lines = ["Columns:", *(line(column) for column in profile.columns)]
    if profile.more_columns:
        lines.append(f"and {profile.more_columns} more columns")
    return lines


def _column_line(column: ColumnProfile) -> str:
    parts = [
        f"- {quoted(column.name)} {one_line(column.type)}",
        f"nulls {column.nulls}",
        f"~distinct {column.distinct}",
    ]
    if column.min is not None and column.max is not None:
        parts.append(f"{one_line(column.min)}..{one_line(column.max)}")
    if column.top_values:
        parts.append(f"top {_json(column.top_values)}")
    return ", ".join(parts)
