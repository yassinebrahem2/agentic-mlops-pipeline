from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from demo_world import paths


def test_folders_come_from_the_environment(isolated_folders: Path) -> None:
    assert paths.source_dir() == isolated_folders / "source"
    assert paths.data_dir() == isolated_folders / "data"
    assert paths.world_dir() == isolated_folders / "world"
    assert paths.mlflow_dir() == isolated_folders / "mlflow"


@pytest.mark.parametrize(
    ("variable", "folder", "name"),
    [
        ("HOTEL_DEMO_SOURCE", paths.source_dir, "source"),
        ("HOTEL_DEMO_DATA", paths.data_dir, "data"),
        ("HOTEL_DEMO_WORLD", paths.world_dir, "world"),
        ("HOTEL_DEMO_MLFLOW", paths.mlflow_dir, "mlflow"),
    ],
)
def test_folders_default_under_the_home_folder(
    monkeypatch: pytest.MonkeyPatch, variable: str, folder: Callable[[], Path], name: str
) -> None:
    monkeypatch.delenv(variable)
    assert folder() == Path.home() / "hotel-demo" / name


def test_the_company_checkout_is_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert paths.company_dir() is None
    monkeypatch.setenv("HOTEL_DEMO_COMPANY", str(tmp_path / "company"))
    assert paths.company_dir() == tmp_path / "company"


def test_day_files_are_named_by_business_date() -> None:
    day = date(2016, 1, 5)
    data = paths.data_dir()
    assert paths.bookings_file(data, day) == data / "bookings" / "2016-01-05.parquet"
    assert paths.labels_file(data, day) == data / "labels" / "2016-01-05.parquet"
