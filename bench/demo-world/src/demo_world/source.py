"""The source data: the CSV loaded once, and each day's bookings and labels built from it.

The two schemas below are the data folder contract (the demo overview's "Data folder layout"):
the one place its columns, their order, types and nullability are written down.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def _required(name: str, type_: pa.DataType) -> pa.Field:
    return pa.field(name, type_, nullable=False)


def _nullable(name: str, type_: pa.DataType) -> pa.Field:
    return pa.field(name, type_, nullable=True)


BOOKINGS_SCHEMA = pa.schema(
    [
        _required("booking_id", pa.int64()),
        _required("booking_date", pa.date32()),
        _required("arrival_date", pa.date32()),
        _required("hotel", pa.string()),
        _required("lead_time", pa.int64()),
        _required("arrival_month", pa.int8()),
        _required("arrival_week", pa.int8()),
        _required("stays_in_weekend_nights", pa.int64()),
        _required("stays_in_week_nights", pa.int64()),
        _required("adults", pa.int64()),
        _nullable("children", pa.int64()),
        _required("babies", pa.int64()),
        _nullable("meal", pa.string()),
        _nullable("country", pa.string()),
        _nullable("market_segment", pa.string()),
        _nullable("distribution_channel", pa.string()),
        _required("is_repeated_guest", pa.int64()),
        _required("previous_cancellations", pa.int64()),
        _required("previous_bookings_not_canceled", pa.int64()),
        _required("reserved_room_type", pa.string()),
        _required("booking_changes", pa.int64()),
        _required("deposit_type", pa.string()),
        _nullable("agent", pa.int64()),
        _nullable("company", pa.int64()),
        _required("customer_type", pa.string()),
        _required("adr", pa.float64()),
        _required("total_of_special_requests", pa.int64()),
    ]
)

LABELS_SCHEMA = pa.schema(
    [
        _required("booking_id", pa.int64()),
        _required("is_canceled", pa.int64()),
        _required("label_date", pa.date32()),
        _required("booking_changes", pa.int64()),
    ]
)

# Each column's own missing marker in the source; "NA" is a marker only for `children`.
_MISSING_MARKERS = {
    "agent": ["NULL"],
    "company": ["NULL"],
    "country": ["NULL"],
    "children": ["NA"],
    "meal": ["Undefined"],
    "market_segment": ["Undefined"],
    "distribution_channel": ["Undefined"],
}
_NULLABLE_INTEGERS = {"children": "Int64", "agent": "Int64", "company": "Int64"}
_MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


def load_source(csv_path: Path) -> pd.DataFrame:
    """Load the source CSV with every derived column the day frames need.

    Dates are midnight timestamps; `booking_changes` keeps its final value.
    """
    frame = pd.read_csv(
        csv_path, keep_default_na=False, na_values=_MISSING_MARKERS, dtype=_NULLABLE_INTEGERS
    )
    # The source row number (0 for the first data row) identifies a booking everywhere.
    frame.insert(0, "booking_id", pd.RangeIndex(len(frame), dtype="int64"))
    arrival = pd.to_datetime(
        pd.DataFrame(
            {
                "year": frame["arrival_date_year"],
                "month": frame["arrival_date_month"].map(_MONTHS),
                "day": frame["arrival_date_day_of_month"],
            }
        )
    )
    booking = arrival - pd.to_timedelta(frame["lead_time"], unit="D")
    frame["arrival_date"] = arrival
    frame["booking_date"] = booking
    frame["arrival_month"] = arrival.dt.month.astype("int8")
    # ISO week of the arrival date, not the source's own week number, which differs.
    frame["arrival_week"] = arrival.dt.isocalendar().week.astype("int8")
    # Overview: released on the cancellation date if cancelled, otherwise on the arrival date,
    # and never before the booking date (a same-day cancellation is released that day).
    status_date = pd.to_datetime(frame["reservation_status_date"], format="%Y-%m-%d")
    released = status_date.where(frame["is_canceled"] == 1, arrival)
    frame["label_date"] = released.where(released >= booking, booking)
    return frame


def bookings_frame(loaded: pd.DataFrame, day: date) -> pd.DataFrame:
    """The bookings made on `day`, as the company sees them at booking time."""
    rows = loaded.loc[loaded["booking_date"] == pd.Timestamp(day), BOOKINGS_SCHEMA.names]
    # No change has happened yet when a booking is made; the final count is in its label.
    rows = rows.assign(booking_changes=0)
    return rows.sort_values("booking_id").reset_index(drop=True)


def labels_frame(loaded: pd.DataFrame, day: date) -> pd.DataFrame:
    """The labels released on `day`, with the final `booking_changes`."""
    rows = loaded.loc[loaded["label_date"] == pd.Timestamp(day), LABELS_SCHEMA.names]
    return rows.sort_values("booking_id").reset_index(drop=True)


def write_frame(frame: pd.DataFrame, schema: pa.Schema, path: Path) -> None:
    """Write `frame` as parquet with exactly `schema`, so a reader never sees half a file."""
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same folder, so the rename is atomic; the suffix keeps it out of `*.parquet` globs.
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        pq.write_table(table.replace_schema_metadata(None), temporary)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
