"""Monitoring project settings and defaults."""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.proposals.payloads import TimeZoneName
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import monitoring_settings

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


async def get_monitoring_settings(databases: Databases, project_id: str) -> MonitoringSettings:
    """Reads every row of monitoring_settings over MonitoringSettings()'s defaults."""
    data = MonitoringSettings().model_dump()
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(select(monitoring_settings.c.key, monitoring_settings.c.value))
        ).all()
    for row in rows:
        key, val_json = row.key, row.value
        if key in data:
            data[key] = json.loads(val_json)
    return MonitoringSettings.model_validate(data)


async def set_monitoring_settings(
    databases: Databases, project_id: str, settings: MonitoringSettings | dict[str, Any]
) -> MonitoringSettings:
    """Validates and writes monitoring settings for a project."""
    if isinstance(settings, MonitoringSettings):
        validated = settings
    else:
        validated = MonitoringSettings.model_validate(settings)
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        for key, val in validated.model_dump().items():
            val_json = json.dumps(val)
            stmt = (
                sqlite_insert(monitoring_settings)
                .values(key=key, value=val_json)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": val_json},
                )
            )
            await conn.execute(stmt)
    return validated
