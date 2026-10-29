"""Scheduler contracts and protocols (I24)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.monitoring.jobs import JobDue, JobKind


class HeartbeatView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]


class SyncObserver(Protocol):
    """Observer for work that follows a tracking sync and a collection."""

    async def synced(self, project_id: str, now: datetime) -> None: ...


class ScheduledJob(Protocol):
    """A job registered with the scheduler."""

    kind: JobKind

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]: ...

    async def run(self, project_id: str, job: JobDue) -> None: ...


class Scheduler(Protocol):
    """The central scheduler loop for a project."""

    def register(self, job: ScheduledJob) -> None: ...

    def add_sync_observer(self, observer: SyncObserver) -> None: ...

    async def run_due(self, project_id: str, now: datetime) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
