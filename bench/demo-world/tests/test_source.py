from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from demo_world import source
from tests.fixture import FIXTURE_CSV, FIXTURE_START

# The overview's data layout, typed out here so the code's schemas are checked against it.
OVERVIEW_BOOKINGS = [
    ("booking_id", pa.int64(), False),
    ("booking_date", pa.date32(), False),
    ("arrival_date", pa.date32(), False),
    ("hotel", pa.string(), False),
    ("lead_time", pa.int64(), False),
    ("arrival_month", pa.int8(), False),
    ("arrival_week", pa.int8(), False),
    ("stays_in_weekend_nights", pa.int64(), False),
    ("stays_in_week_nights", pa.int64(), False),
    ("adults", pa.int64(), False),
    ("children", pa.int64(), True),
    ("babies", pa.int64(), False),
    ("meal", pa.string(), True),
    ("country", pa.string(), True),
    ("market_segment", pa.string(), True),
    ("distribution_channel", pa.string(), True),
    ("is_repeated_guest", pa.int64(), False),
    ("previous_cancellations", pa.int64(), False),
    ("previous_bookings_not_canceled", pa.int64(), False),
    ("reserved_room_type", pa.string(), False),
    ("booking_changes", pa.int64(), False),
    ("deposit_type", pa.string(), False),
    ("agent", pa.int64(), True),
    ("company", pa.int64(), True),
    ("customer_type", pa.string(), False),
    ("adr", pa.float64(), False),
    ("total_of_special_requests", pa.int64(), False),
]
OVERVIEW_LABELS = [
    ("booking_id", pa.int64(), False),
    ("is_canceled", pa.int64(), False),
    ("label_date", pa.date32(), False),
    ("booking_changes", pa.int64(), False),
]


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


def _fields(schema: pa.Schema) -> list[tuple[str, pa.DataType, bool]]:
    return [(f.name, f.type, f.nullable) for f in schema]


def _row(loaded: pd.DataFrame, booking_id: int) -> pd.Series:
    return loaded.loc[loaded["booking_id"] == booking_id].iloc[0]


def test_the_schemas_are_the_overviews() -> None:
    assert _fields(source.BOOKINGS_SCHEMA) == OVERVIEW_BOOKINGS
    assert _fields(source.LABELS_SCHEMA) == OVERVIEW_LABELS


def test_a_days_bookings_frame_has_exactly_the_overviews_columns(loaded: pd.DataFrame) -> None:
    bookings = source.bookings_frame(loaded, FIXTURE_START)

    assert list(bookings.columns) == [name for name, _, _ in OVERVIEW_BOOKINGS]
    assert bookings["booking_id"].tolist() == [11, 12]


def test_a_days_labels_frame_has_exactly_the_overviews_columns(loaded: pd.DataFrame) -> None:
    labels = source.labels_frame(loaded, FIXTURE_START)

    assert list(labels.columns) == [name for name, _, _ in OVERVIEW_LABELS]
    assert labels["booking_id"].tolist() == [9, 11, 12]


def test_booking_changes_is_zero_in_bookings_and_final_in_labels(loaded: pd.DataFrame) -> None:
    day = date(2016, 1, 4)
    bookings = source.bookings_frame(loaded, day)
    # Booking 17 (booked 2016-01-04) changed three times before its stay.
    labels = source.labels_frame(loaded, date(2017, 1, 1))

    assert bookings["booking_changes"].tolist() == [0, 0]
    assert labels.set_index("booking_id")["booking_changes"].to_dict() == {17: 3}


def test_written_files_have_exactly_the_overviews_parquet_schema(
    loaded: pd.DataFrame, tmp_path: Path
) -> None:
    bookings_path = tmp_path / "bookings" / "2016-01-04.parquet"
    labels_path = tmp_path / "labels" / "2016-01-05.parquet"

    source.write_frame(
        source.bookings_frame(loaded, date(2016, 1, 4)), source.BOOKINGS_SCHEMA, bookings_path
    )
    source.write_frame(
        source.labels_frame(loaded, date(2016, 1, 5)), source.LABELS_SCHEMA, labels_path
    )

    assert _fields(pq.read_schema(bookings_path)) == OVERVIEW_BOOKINGS
    assert _fields(pq.read_schema(labels_path)) == OVERVIEW_LABELS
    assert pq.read_table(labels_path).to_pydict() == {
        "booking_id": [16, 18],
        "is_canceled": [1, 1],
        "label_date": [date(2016, 1, 5), date(2016, 1, 5)],
        "booking_changes": [0, 0],
    }
    assert [p.name for p in bookings_path.parent.iterdir()] == ["2016-01-04.parquet"]


def test_nulls_are_written_as_nulls(loaded: pd.DataFrame, tmp_path: Path) -> None:
    path = tmp_path / "2015-12-20.parquet"

    source.write_frame(
        source.bookings_frame(loaded, date(2015, 12, 20)), source.BOOKINGS_SCHEMA, path
    )

    written = pq.read_table(path).to_pydict()
    assert written["booking_id"] == [6, 7]
    assert written["market_segment"] == [None, "Online TA"]
    assert written["agent"] == [9, 9]
    assert written["company"] == [None, None]
    assert written["arrival_week"] == [53, 53]


def test_the_missing_markers_become_nulls_and_nothing_else_does(loaded: pd.DataFrame) -> None:
    assert pd.isna(_row(loaded, 1)["agent"])
    assert pd.isna(_row(loaded, 1)["company"])
    assert pd.isna(_row(loaded, 2)["children"])
    assert pd.isna(_row(loaded, 4)["meal"])
    assert pd.isna(_row(loaded, 5)["country"])
    assert pd.isna(_row(loaded, 6)["market_segment"])
    assert pd.isna(_row(loaded, 6)["distribution_channel"])
    nulls = {"agent": 2, "company": 20, "children": 1, "meal": 1, "country": 1}
    nulls |= {"market_segment": 1, "distribution_channel": 1}
    assert {column: int(loaded[column].isna().sum()) for column in nulls} == nulls
    assert int(loaded.drop(columns=list(nulls)).isna().sum().sum()) == 0
    assert _row(loaded, 0)["agent"] == 240
    assert _row(loaded, 18)["company"] == 45


def test_dates_come_from_the_arrival_and_the_lead_time(loaded: pd.DataFrame) -> None:
    long_lead = _row(loaded, 0)

    assert long_lead["arrival_date"] == pd.Timestamp("2015-07-01")
    assert long_lead["booking_date"] == pd.Timestamp("2014-03-10")
    assert long_lead["arrival_month"] == 7


@pytest.mark.parametrize(
    ("booking_id", "week"),
    [(5, 53), (11, 53), (13, 1), (17, 52)],  # 2015-12-28, 2016-01-01, 2016-01-04, 2017-01-01
)
def test_the_arrival_week_is_the_iso_week(loaded: pd.DataFrame, booking_id: int, week: int) -> None:
    assert _row(loaded, booking_id)["arrival_week"] == week


@pytest.mark.parametrize(
    ("booking_id", "released"),
    [
        (0, "2015-07-01"),  # checked out: its arrival date, not the status date
        (1, "2015-07-20"),  # cancelled: its cancellation date
        (3, "2015-10-05"),  # cancelled the day it was made: that day
        (12, "2016-01-01"),  # same, inside the booking days
        (4, "2015-12-10"),  # no-show: its status date, the arrival
        (10, "2015-08-10"),  # status date before the booking date: never before the booking
    ],
)
def test_labels_are_released_by_the_overviews_rule(
    loaded: pd.DataFrame, booking_id: int, released: str
) -> None:
    assert _row(loaded, booking_id)["label_date"] == pd.Timestamp(released)


def test_no_label_is_released_before_its_booking(loaded: pd.DataFrame) -> None:
    assert bool((loaded["label_date"] >= loaded["booking_date"]).all())
