"""Where the world reads and writes: every folder is overridable by an environment variable."""

import os
from datetime import date
from pathlib import Path

SOURCE_VARIABLE = "HOTEL_DEMO_SOURCE"
DATA_VARIABLE = "HOTEL_DEMO_DATA"
WORLD_VARIABLE = "HOTEL_DEMO_WORLD"
MLFLOW_VARIABLE = "HOTEL_DEMO_MLFLOW"
COMPANY_VARIABLE = "HOTEL_DEMO_COMPANY"
# Read by the company and MLflow, never chosen by the world: under `up`, the tracking server.
TRACKING_URI_VARIABLE = "MLFLOW_TRACKING_URI"


def _folder(variable: str, name: str) -> Path:
    # Read at call time, so a run (or a test) can point the world elsewhere after import.
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / "hotel-demo" / name


def source_dir() -> Path:
    """The downloaded source CSV, shared by every run."""
    return _folder(SOURCE_VARIABLE, "source")


def data_dir() -> Path:
    """The company's production data folder."""
    return _folder(DATA_VARIABLE, "data")


def world_dir() -> Path:
    """The world's own folder (state, answer key, planted flaws), never inside the data folder."""
    return _folder(WORLD_VARIABLE, "world")


def mlflow_dir() -> Path:
    """The company's MLflow folder (its SQLite store and artifacts)."""
    return _folder(MLFLOW_VARIABLE, "mlflow")


def company_dir() -> Path | None:
    """The checkout of the demo repository the world runs, or None to play the files only.

    Opt-in, with no default: without it the loop writes the day's files and runs nothing.
    """
    value = os.environ.get(COMPANY_VARIABLE)
    return Path(value) if value else None


def bookings_file(data: Path, day: date) -> Path:
    return data / "bookings" / f"{day.isoformat()}.parquet"


def labels_file(data: Path, day: date) -> Path:
    return data / "labels" / f"{day.isoformat()}.parquet"
