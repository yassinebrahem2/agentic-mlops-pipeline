"""Verdicts and baseline contracts and protocols (I31)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from mlops_agents.monitoring.results.contract import ResultCategory

VerdictValue = Literal["real_issue", "not_an_issue", "wrong_type"]


class VerdictDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: VerdictValue
    right_category: ResultCategory | None = None
    right_subtype: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> VerdictDraft:
        if self.value == "wrong_type":
            if self.right_category is None:
                raise ValueError("right_category is required when value is 'wrong_type'")
        else:
            if self.right_category is not None:
                raise ValueError("right_category must not be set when value is not 'wrong_type'")
        if self.right_subtype is not None and self.right_category is None:
            raise ValueError("right_subtype can only be set when right_category is set")
        return self


class VerdictView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    value: VerdictValue
    right_category: ResultCategory | None = None
    right_subtype: str | None = None
    source: Literal["user", "labels"]
    check_versions: dict[str, str] = {}
    note: str | None = None
    created: datetime


class MissReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    category: ResultCategory | None = None
    occurred_at: datetime
    reported_at: datetime


class BaselineLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    team_check_id: str
    first_fired_at: datetime
    seen_at: datetime
    lead_seconds: float


SilentVerdict = Literal["real_issue", "not_an_issue"]


class SilentTeamAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str
    check_id: str
    subject: str
    ran_at: datetime
    verdict: SilentVerdict | None = None


class Verdicts(Protocol):
    async def record(
        self, project_id: str, incident_id: str, draft: VerdictDraft, by: str
    ) -> VerdictView: ...

    async def get(self, project_id: str, incident_id: str) -> VerdictView | None: ...

    async def confirm_by_labels(self, project_id: str) -> list[VerdictView]: ...

    async def report_miss(self, project_id: str, miss: MissReport) -> None: ...

    async def list(self, project_id: str, since: datetime | None = None) -> list[VerdictView]: ...

    async def list_misses(
        self, project_id: str, since: datetime | None = None
    ) -> list[MissReport]: ...


class Baseline(Protocol):
    async def available(self, project_id: str) -> bool: ...

    async def links(self, project_id: str, incident_id: str) -> list[BaselineLink]: ...

    async def silent_alerts(self, project_id: str) -> list[SilentTeamAlert]: ...

    async def matched_incidents(self, project_id: str) -> set[str]: ...

    async def judged_silent_alerts(self, project_id: str) -> list[SilentTeamAlert]: ...

    async def judge_silent(
        self, project_id: str, result_id: str, value: SilentVerdict, by: str
    ) -> SilentTeamAlert: ...
