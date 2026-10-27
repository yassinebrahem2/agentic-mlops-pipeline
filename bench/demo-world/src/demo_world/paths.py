"""Where the world reads and writes: every folder is overridable by an environment variable."""

import os
from datetime import date
from pathlib import Path


def _folder(variable: str, name: str) -> Path:
    # Read at call time, so a run (or a test) can point the world elsewhere after import.
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / "hotel-demo" / name


def source_dir() -> Path:
    """The downloaded source CSV, shared by every run."""
    return _folder("HOTEL_DEMO_SOURCE", "source")


def data_dir() -> Path:
    """The company's production data folder."""
    return _folder("HOTEL_DEMO_DATA", "data")


def world_dir() -> Path:
    """The world's own folder (state, answer key, planted flaws), never inside the data folder."""
    return _folder("HOTEL_DEMO_WORLD", "world")


def bookings_file(data: Path, day: date) -> Path:
    return data / "bookings" / f"{day.isoformat()}.parquet"


def labels_file(data: Path, day: date) -> Path:
    return data / "labels" / f"{day.isoformat()}.parquet"
