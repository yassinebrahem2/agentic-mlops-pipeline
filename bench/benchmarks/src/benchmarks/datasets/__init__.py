"""Datasets registry for benchmarks."""

from __future__ import annotations

from collections.abc import Callable

from benchmarks.datasets.contract import Dataset
from benchmarks.datasets.ulb import ULBDataset

DATASETS: dict[str, Callable[[], Dataset]] = {"ulb": ULBDataset}

__all__ = ["DATASETS", "Dataset", "ULBDataset"]
