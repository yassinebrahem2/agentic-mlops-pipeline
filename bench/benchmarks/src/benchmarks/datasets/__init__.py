"""Datasets registry for benchmarks."""

from __future__ import annotations

from collections.abc import Callable

from benchmarks.datasets.contract import Dataset
from benchmarks.datasets.drift import DriftDataset
from benchmarks.datasets.telemetry import TelemetryDataset
from benchmarks.datasets.ulb import ULBDataset

DATASETS: dict[str, Callable[..., Dataset]] = {
    "drift": DriftDataset,
    "telemetry": TelemetryDataset,
    "ulb": ULBDataset,
}

__all__ = ["DATASETS", "Dataset", "DriftDataset", "TelemetryDataset", "ULBDataset"]
