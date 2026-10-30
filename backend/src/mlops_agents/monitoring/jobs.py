"""Monitoring jobs and scheduler kinds."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class JobKind(StrEnum):
    SYNC_TRACKING = "sync_tracking"
    SYNC_PRS = "sync_prs"
    COLLECT_RESULTS = "collect_results"
    AFTER_SYNC = "after_sync"
    CHECK_EXPECTATIONS = "check_expectations"
    FOLLOW_UPS = "follow_ups"
    RETRY_NOTIFICATIONS = "retry_notifications"
    ESCALATE = "escalate"
    MORNING_REPORT = "morning_report"


JOB_ORDER: Final[tuple[JobKind, ...]] = tuple(JobKind)


class JobDue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: JobKind
    due: datetime
    subject: str = ""
