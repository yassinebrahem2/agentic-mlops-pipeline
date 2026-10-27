"""The company's history: every bookings and labels file dated before the simulation starts."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from demo_world import paths, source


class BackfillError(Exception):
    """The backfill cannot run: the source is missing or the data folder already has files."""


@dataclass(frozen=True)
class Summary:
    bookings_files: int
    labels_files: int


def _days_before(dates: pd.Series, until: date) -> list[date]:
    # No lower bound: long-lead bookings made before the first arrivals are history too.
    return sorted(day.date() for day in dates[dates < pd.Timestamp(until)].unique())


def backfill(loaded: pd.DataFrame, data: Path, until: date) -> Summary:
    """Write the bookings of every booking date and the labels of every release date before `until`."""
    if data.exists() and any(p.is_file() for p in data.rglob("*")):
        raise BackfillError(
            f"the data folder {data} already has files; backfill needs an empty one"
        )

    booking_days = _days_before(loaded["booking_date"], until)
    for day in booking_days:
        frame = source.bookings_frame(loaded, day)
        source.write_frame(frame, source.BOOKINGS_SCHEMA, paths.bookings_file(data, day))
    label_days = _days_before(loaded["label_date"], until)
    for day in label_days:
        frame = source.labels_frame(loaded, day)
        source.write_frame(frame, source.LABELS_SCHEMA, paths.labels_file(data, day))
    return Summary(bookings_files=len(booking_days), labels_files=len(label_days))
