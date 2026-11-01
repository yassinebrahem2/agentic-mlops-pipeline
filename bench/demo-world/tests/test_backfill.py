from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from demo_world import backfill, paths, source
from tests.fixture import FIXTURE_CSV, FIXTURE_START

# Every booking date of the fixture before its start, the long-lead one of 2014 included.
EXPECTED_BOOKINGS = [
    "2014-03-10",
    "2015-06-01",
    "2015-08-10",
    "2015-09-01",
    "2015-10-05",
    "2015-11-01",
    "2015-11-10",
    "2015-12-01",
    "2015-12-20",
    "2015-12-31",
]
# Every release date before the start; the label released on the start day itself is not.
EXPECTED_LABELS = [
    "2015-07-01",
    "2015-07-20",
    "2015-08-10",
    "2015-09-01",
    "2015-10-05",
    "2015-12-10",
    "2015-12-28",
    "2015-12-31",
]


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _ids(folder: Path) -> set[int]:
    return {i for p in folder.iterdir() for i in pq.read_table(p).column("booking_id").to_pylist()}


def test_backfill_writes_every_booking_and_release_date_before_until(
    loaded: pd.DataFrame,
) -> None:
    data = paths.data_dir()

    summary = backfill.backfill(loaded, data, FIXTURE_START)

    assert _names(data) == ["bookings", "labels"]
    assert _names(data / "bookings") == [f"{day}.parquet" for day in EXPECTED_BOOKINGS]
    assert _names(data / "labels") == [f"{day}.parquet" for day in EXPECTED_LABELS]
    assert summary == backfill.Summary(bookings_files=10, labels_files=8)


def test_backfill_writes_each_booking_once_and_only_labels_of_written_bookings(
    loaded: pd.DataFrame,
) -> None:
    data = paths.data_dir()

    backfill.backfill(loaded, data, FIXTURE_START)

    booked = _ids(data / "bookings")
    assert booked == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
    assert _ids(data / "labels") <= booked
    assert pq.read_table(data / "bookings" / "2015-12-20.parquet").num_rows == 2


def test_backfill_refuses_a_data_folder_that_has_files(loaded: pd.DataFrame) -> None:
    data = paths.data_dir()
    (data / "jobs").mkdir(parents=True)
    (data / "jobs" / "2015-12-31.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(backfill.BackfillError, match="already has files"):
        backfill.backfill(loaded, data, FIXTURE_START)

    assert _names(data) == ["jobs"]


def test_backfill_accepts_an_empty_existing_folder(loaded: pd.DataFrame) -> None:
    data = paths.data_dir()
    (data / "bookings").mkdir(parents=True)

    backfill.backfill(loaded, data, date(2015, 7, 2))

    assert _names(data / "bookings") == ["2014-03-10.parquet", "2015-06-01.parquet"]
    assert _names(data / "labels") == ["2015-07-01.parquet"]
