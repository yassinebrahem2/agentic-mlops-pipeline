"""Activity route: the project's events and audit records, newest first."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.events.activity import (
    ACTIVITY_LIMIT_DEFAULT,
    ACTIVITY_LIMIT_MAX,
    ActivityPage,
    activity_page,
)
from mlops_agents.events.contract import EventType

router = APIRouter(tags=["activity"])


@router.get("/projects/{project_id}/activity", response_model=ActivityPage)
async def get_activity(
    project: ProjectDep,
    services: ServicesDep,
    type: EventType | None = None,
    source: str | None = None,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=ACTIVITY_LIMIT_MAX)] = ACTIVITY_LIMIT_DEFAULT,
) -> ActivityPage:
    return await activity_page(
        services.events,
        services.audit,
        project.id,
        type=type,
        source=source,
        before=before,
        limit=limit,
    )
