"""Dataset protocol for benchmarks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Protocol

from demo_world.answer_key import Record
from mlops_agents.proposals.payloads import HandoffPayload


class Dataset(Protocol):
    """Protocol for benchmark datasets."""

    name: str

    def days(self) -> Sequence[date]:
        """Sequence of business dates for this dataset, in chronological order."""
        ...

    def handoff(self) -> HandoffPayload:
        """The handoff payload defining feeds, checks, and operations."""
        ...

    def prepare(self, cache: Path) -> None:
        """Fetches, verifies or generates cached dataset assets. Idempotent."""
        ...

    def write_day(self, day: date, data_dir: Path, results_dir: Path) -> None:
        """Writes that day's input files and check results into the run folders."""
        ...

    def key(self) -> Sequence[Record]:
        """The ground truth answer key records for scoring."""
        ...
