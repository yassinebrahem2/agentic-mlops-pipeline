"""GitHub work routes: issues settings, and later reviews and activity."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mlops_agents.agents.tools.issues import (
    IssuePriority,
    get_issue_min_priority,
    set_issue_min_priority,
)
from mlops_agents.api.deps import ProjectDep, ServicesDep

router = APIRouter(tags=["github-work"])


class IssuesSettings(BaseModel):
    min_priority: IssuePriority


@router.get("/projects/{project_id}/settings/issues", response_model=IssuesSettings)
async def get_issues_settings(
    project: ProjectDep,
    services: ServicesDep,
) -> IssuesSettings:
    min_priority = await get_issue_min_priority(services.databases, project.id)
    return IssuesSettings(min_priority=min_priority)


@router.put("/projects/{project_id}/settings/issues", response_model=IssuesSettings)
async def update_issues_settings(
    project: ProjectDep,
    body: IssuesSettings,
    services: ServicesDep,
) -> IssuesSettings:
    stored = await set_issue_min_priority(services.databases, project.id, body.min_priority)
    return IssuesSettings(min_priority=stored)
