"""Stats contracts and protocols (I41)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.monitoring.results.contract import ResultCategory

STATS_MIN_VERDICTS: Final[int] = 5

StatsWindow = Literal["7d", "30d", "all"]
StatGroup = Literal[
    "detection",
    "speed",
    "noise",
    "diagnosis",
    "effort",
    "model_impact",
    "cost",
    "reliability",
]

STAT_METRICS: Final[Mapping[StatGroup, tuple[str, ...]]] = {
    "detection": (
        "precision",
        "caught_baseline_missed",
        "false_alarms_avoided",
        "misses",
    ),
    "speed": ("detection_delay_s", "time_to_resolution_s"),
    "noise": ("alerts_per_week", "signals_per_incident"),
    "diagnosis": ("bad_data_vs_drift_correct",),
    "effort": (
        "autonomous_share",
        "approvals_requested",
        "acceptance_rate",
        "prs_opened",
        "prs_merged",
        "prs_closed",
    ),
    "model_impact": ("metric_before", "metric_after"),
    "cost": ("tokens_per_agent", "cost_per_incident"),
    "reliability": ("failed_runs", "tool_errors", "sandbox_failures"),
}


class StatValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    n: int
    collecting: bool


class StatsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: StatsWindow
    category: ResultCategory | None = None
    baseline_available: bool
    groups: dict[StatGroup, dict[str, StatValue]]


class Stats(Protocol):
    async def compute(
        self,
        project_id: str,
        window: StatsWindow,
        category: ResultCategory | None,
        now: datetime,
    ) -> StatsView: ...
