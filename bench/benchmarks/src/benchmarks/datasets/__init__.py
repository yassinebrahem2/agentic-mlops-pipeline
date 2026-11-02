"""Datasets registry for benchmarks."""

from __future__ import annotations

from collections.abc import Callable

from benchmarks.datasets.contract import Dataset

DATASETS: dict[str, Callable[[], Dataset]] = {}

__all__ = ["DATASETS", "Dataset"]
