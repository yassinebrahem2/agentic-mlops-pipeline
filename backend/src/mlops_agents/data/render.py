"""The data summaries as text: a table's profile, and the lines of the `data_summary` hook.

Names, file names, values and error reasons come from the team's files: each goes through
`quoted` or `one_line`, so it stays on its own line and a line of it cannot pass for one of
ours. The hook registry escapes data tags in the whole text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from itertools import takewhile
from pathlib import Path
from typing import Final

from mlops_agents.connections.contract import ConnectionView, TableRef
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.data.constants import LISTED_FILES, LISTED_TABLE_NAMES, TEXT_CUT
from mlops_agents.data.profile import ColumnProfile, TableProfile

SIZE_CAP: Final = "size cap"
TIME_BUDGET: Final = "time budget"
CACHE_UNAVAILABLE: Final = "profile cache unavailable"
DENYLISTED_FILE: Final = "looks like a secret file"
UNKNOWN_ROW_COUNT: Final = "row count unknown (timed out)"
_REASON_CUT: Final = TEXT_CUT * 2
# DuckDB's CSV errors give only the place on their first line. The cause is the last line
# before a blank line or DuckDB's advice ("Possible fixes:", "Possible Solution:"); a copy
# of the team's row, which can span lines, may come before it (Decision #431).
_PLACE_ONLY: Final = re.compile(r"CSV Error on Line: \d+$")
_ADVICE: Final = "Possible "
_ROW_COPY: Final = "Original Line:"

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


def _cause(reason: str) -> str:
    """The line of `reason` naming its cause, cut to `TEXT_CUT * 2` characters: DuckDB's
    whole text copies the file's lines and its settings (Decisions #430, #431)."""
    lines = reason.splitlines()
    first = lines[0] if lines else ""
    if _PLACE_ONLY.search(first):
        body = list(
            takewhile(lambda line: line.strip() and not line.startswith(_ADVICE), lines[1:])
        )
        if body and not body[-1].startswith(_ROW_COPY):
            first = f"{first}: {body[-1].strip()}"
    return first[:_REASON_CUT]


def unreachable_line(view: ConnectionView, detail: str | None) -> str:
    """The detail cut as an unreadable table's reason is."""
    return (
        f"Connection {quoted(view.name)} ({one_line(view.kind)}, unreachable): "
        f"{one_line(_cause(detail or ''))}"
    )


def no_tables_line() -> str:
    return "No tables."


def left_out_line(ref: TableRef, reason: str) -> str:
    return f"Table {quoted(ref.name)} ({ref.format}): detail left out: {reason}"


def unreadable_line(ref: TableRef, reason: str) -> str:
    return f"Table {quoted(ref.name)} ({ref.format}): unreadable: {one_line(_cause(reason))}"


def reason_without_folders(reason: str, folders: Sequence[Path | str], mount: str) -> str:
    """`reason` with each host folder replaced by `mount`, the connection's path in the
    sandbox: a `Path` as DuckDB or Python writes it, a `str` also as the user typed it."""
    forms: set[str] = set()
    for folder in folders:
        if isinstance(folder, str) and not folder.strip():
            continue  # Path("") is ".", which would rewrite every dot
        path = Path(folder)
        forms |= {path.as_posix(), str(path)}
        if isinstance(folder, str):
            forms.add(folder)
    for form in sorted(forms, key=len, reverse=True):
        reason = reason.replace(form, mount)
    return reason


def left_out_entries_line(count: int) -> str:
    """The one line counting the connections and tables past the cap."""
    return f"and {count} more entries left out: {SIZE_CAP}"


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
    rows = UNKNOWN_ROW_COUNT if profile.row_count is None else _rows(profile.row_count)
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


def _rows(count: int) -> str:
    return "1 row" if count == 1 else f"{count} rows"


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
