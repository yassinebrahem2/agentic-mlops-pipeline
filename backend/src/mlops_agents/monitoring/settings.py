"""Monitoring project settings and defaults."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from mlops_agents.proposals.payloads import TimeZoneName

MAX_WAKES_PER_HOUR: Final[int] = 30


class MonitoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_interval_s: int = 60
    grouping_window_minutes: int = 60
    reopen_window_hours: int = 24
    recent_change_window_hours: int = 24
    escalation_timeout_minutes: int = 15
    check_grace_minutes: int = 30
    second_deadline_minutes: int = 60
    parser_disable_after: int = 3
    evaluation_max_age_hours: int = 72
    circuit_breaker_fixes: int = 3
    circuit_breaker_window_days: int = 30
    timezone: TimeZoneName | None = None

    @field_validator("collection_interval_s")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        if v < 10:
            raise ValueError(f"collection_interval_s must be at least 10, got {v}")
        return v
