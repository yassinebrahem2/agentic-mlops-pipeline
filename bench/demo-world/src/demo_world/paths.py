"""Where the world reads and writes: every folder is overridable by an environment variable."""

import os
from datetime import date
from pathlib import Path

SOURCE_VARIABLE = "HOTEL_DEMO_SOURCE"
DATA_VARIABLE = "HOTEL_DEMO_DATA"
WORLD_VARIABLE = "HOTEL_DEMO_WORLD"
MLFLOW_VARIABLE = "HOTEL_DEMO_MLFLOW"
COMPANY_VARIABLE = "HOTEL_DEMO_COMPANY"
# The company's repository on GitHub (`owner/name`): a run's own copy, never the original.
REPO_VARIABLE = "HOTEL_DEMO_REPO"
MLFLOW_PORT_VARIABLE = "HOTEL_DEMO_MLFLOW_PORT"
# Read by the company and MLflow, never chosen by the world: under `up`, the tracking server.
TRACKING_URI_VARIABLE = "MLFLOW_TRACKING_URI"
# Every setting a run writes to its run.env, in that order.
RUN_SETTINGS = (
    REPO_VARIABLE,
    COMPANY_VARIABLE,
    DATA_VARIABLE,
    MLFLOW_VARIABLE,
    WORLD_VARIABLE,
    MLFLOW_PORT_VARIABLE,
)
# The demo repository itself: the template every run copies, which no run ever changes.
ORIGINAL_REPOSITORY = "yassinebrahem2/hotel-cancellation-demo"
# The overview's default port for the company's MLflow server.
DEFAULT_MLFLOW_PORT = 5000


def _home() -> Path:
    return Path.home() / "hotel-demo"


def _folder(variable: str, name: str) -> Path:
    # Read at call time, so a run (or a test) can point the world elsewhere after import.
    value = os.environ.get(variable)
    return Path(value) if value else _home() / name


def runs_dir() -> Path:
    """Where `new-run` puts each run: its company clone, its folders and its run.env."""
    return _home() / "runs"


def repository() -> str | None:
    """The company's repository on GitHub, or None: then the world touches nothing there."""
    return os.environ.get(REPO_VARIABLE) or None


def mlflow_port() -> int:
    """The port of the company's MLflow server under `up`."""
    value = os.environ.get(MLFLOW_PORT_VARIABLE)
    return int(value) if value else DEFAULT_MLFLOW_PORT


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
