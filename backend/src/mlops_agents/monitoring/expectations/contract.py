"""Expectations, checks and maintenance protocol (I25)."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.proposals.payloads import Cadence, FileResults, MlflowResults

SECOND_DEADLINE_KEY: Final = "second_deadline"


class ExpectationKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["feed", "job", "check", "training_request"]
    ref: str
    model: str | None = None


class ExpectationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: ExpectationKey
    category: ResultCategory
    cadence: Cadence | None = None
    due_at: datetime | None = None
    timezone: str
    grace_minutes: int | None = None
    second_deadline: bool = False
    activates_on_pr: int | None = None
    source: Literal["handoff", "check", "training"]

    @model_validator(mode="after")
    def _validate_timing(self) -> ExpectationSpec:
        if (self.cadence is None and self.due_at is None) or (
            self.cadence is not None and self.due_at is not None
        ):
            raise ValueError("ExpectationSpec must have exactly one of cadence or due_at")
        return self


class ExpectationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: ExpectationKey
    category: ResultCategory
    active: bool
    next_deadline: datetime | None = None
    last_seen: datetime | None = None
    status: Literal["ok", "due", "late", "silent", "suspended", "pending_pr"]
    since: datetime | None = None


class CheckView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    check_id: str
    category: ResultCategory
    cadence: Cadence
    timezone: str
    results_location: FileResults | MlflowResults
    pr_number: int
    agent: str
    version: str | None = None
    status: Literal["pending_pr", "active", "retired"]
    registered: datetime


class Maintenance(Protocol):
    async def suspended(
        self, project_id: str, scope: ExpectationKey | str, at: datetime
    ) -> bool: ...


class NeverSuspended:
    """Contracts default Maintenance implementation: never suspended."""

    async def suspended(self, project_id: str, scope: ExpectationKey | str, at: datetime) -> bool:
        return False


class Expectations(Protocol):
    async def register(self, project_id: str, spec: ExpectationSpec) -> None: ...

    async def retire(self, project_id: str, key: ExpectationKey) -> None: ...

    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None: ...

    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]: ...

    async def evaluate(self, project_id: str, now: datetime) -> list[str]: ...

    async def states(self, project_id: str) -> list[ExpectationState]: ...
