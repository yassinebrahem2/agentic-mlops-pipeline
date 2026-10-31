"""Verdicts and miss reports router (I31)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.verdicts.contract import MissReport, VerdictDraft, VerdictView

router = APIRouter(tags=["verdicts"])


class MissDraft(BaseModel):
    """A person's report of a problem nothing caught; the server stamps `reported_at`."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)
    category: ResultCategory | None = None
    occurred_at: datetime


@router.post(
    "/projects/{project_id}/incidents/{incident_id}/verdict",
    response_model=VerdictView,
)
async def record_verdict(
    project: ProjectDep,
    incident_id: str,
    body: VerdictDraft,
    services: ServicesDep,
) -> VerdictView:
    return await services.verdicts.record(project.id, incident_id, body, by="user")


@router.get(
    "/projects/{project_id}/verdicts",
    response_model=list[VerdictView],
)
async def list_verdicts(
    project: ProjectDep,
    services: ServicesDep,
    since: Annotated[datetime | None, Query()] = None,
) -> list[VerdictView]:
    return await services.verdicts.list(project.id, since=since)


@router.post(
    "/projects/{project_id}/misses",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def report_miss(
    project: ProjectDep,
    body: MissDraft,
    services: ServicesDep,
) -> Response:
    miss = MissReport(
        description=body.description,
        category=body.category,
        occurred_at=body.occurred_at,
        reported_at=services.clock.now(),
    )
    await services.verdicts.report_miss(project.id, miss)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/misses",
    response_model=list[MissReport],
)
async def list_misses(
    project: ProjectDep,
    services: ServicesDep,
    since: Annotated[datetime | None, Query()] = None,
) -> list[MissReport]:
    return await services.verdicts.list_misses(project.id, since=since)
