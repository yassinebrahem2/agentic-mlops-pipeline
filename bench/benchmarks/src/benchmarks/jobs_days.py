"""Business day starts extracted from the demo company's jobs log files."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast


def day_starts_from_jobs(
    jobs_dir: Path, date_range: tuple[date, date] | None = None
) -> tuple[list[tuple[date, datetime]], list[date]]:
    """Determine the earliest start timestamp (UTC) for each business date from job files.

    Scans `jobs_dir` for files named `<YYYY-MM-DD>.jsonl`.
    Each line in a job file is JSON with a `"started"` ISO timestamp.

    If `date_range` is given, checks each calendar date from `date_range[0]` to `date_range[1]` (inclusive).
    If `date_range` is None, finds all `<YYYY-MM-DD>.jsonl` files in `jobs_dir`; if none, returns `([], [])`.
    Otherwise, evaluates calendar days from `min(file_dates)` to `max(file_dates)` (inclusive).

    Dates with no file, an empty file, or no valid `"started"` timestamp are returned in `gaps`.
    Dates with at least one valid `"started"` timestamp are returned in `starts` with their earliest
    timestamp in UTC.

    Returns `(starts, gaps)` sorted ascending by date.
    """
    if date_range is not None:
        start_date, end_date = date_range
        if start_date > end_date:
            return ([], [])
    else:
        file_dates: set[date] = set()
        if jobs_dir.is_dir():
            for entry in jobs_dir.iterdir():
                if entry.is_file() and entry.name.endswith(".jsonl"):
                    stem = entry.name[:-6]
                    try:
                        d = date.fromisoformat(stem)
                        file_dates.add(d)
                    except ValueError:
                        continue
        if not file_dates:
            return ([], [])
        start_date = min(file_dates)
        end_date = max(file_dates)

    starts: list[tuple[date, datetime]] = []
    gaps: list[date] = []

    curr = start_date
    while curr <= end_date:
        file_path = jobs_dir / f"{curr.isoformat()}.jsonl"
        if not file_path.is_file():
            gaps.append(curr)
        else:
            earliest: datetime | None = None
            try:
                with file_path.open("r", encoding="utf-8") as file:
                    for line in file:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            raw: object = json.loads(line_str)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(raw, dict):
                            continue
                        record: dict[str, object] = cast(dict[str, object], raw)
                        started_val = record.get("started")
                        if not isinstance(started_val, str):
                            continue
                        try:
                            dt = datetime.fromisoformat(started_val)
                        except ValueError:
                            continue
                        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                        if earliest is None or dt < earliest:
                            earliest = dt
            except OSError:
                pass

            if earliest is None:
                gaps.append(curr)
            else:
                starts.append((curr, earliest))

        curr += timedelta(days=1)

    return (starts, gaps)
