"""GitHub work routes: issues settings, reviews, and activity."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mlops_agents.agents.reviews import (
    GitHubItem,
    LocalReview,
    ReviewDecisionRequest,
    decide_local_review,
    get_github_activity,
    list_local_reviews,
)
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tools.issues import (
    IssuePriority,
    get_issue_min_priority,
    set_issue_min_priority,
)
from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.integrations.github.git import GitRunner

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


@router.get("/projects/{project_id}/reviews", response_model=list[LocalReview])
async def get_local_reviews(
    project: ProjectDep,
    services: ServicesDep,
) -> list[LocalReview]:
    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    return await list_local_reviews(
        tasks=services.tasks,
        workspace=services.workspace,
        git=git,
        empty_hooks=services.paths.empty_hooks,
        project_id=project.id,
    )


@router.post("/projects/{project_id}/reviews/{task_id}/decision", response_model=TaskView)
async def decide_review(
    project: ProjectDep,
    task_id: str,
    body: ReviewDecisionRequest,
    services: ServicesDep,
) -> TaskView:
    return await decide_local_review(
        tasks=services.tasks,
        sessions=services.sessions,
        project_id=project.id,
        task_id=task_id,
        approve=body.approve,
        note=body.note,
    )


@router.get("/projects/{project_id}/github/activity", response_model=list[GitHubItem])
async def get_github_activity_route(
    project: ProjectDep,
    services: ServicesDep,
) -> list[GitHubItem]:
    return await get_github_activity(
        events=services.events,
        tasks=services.tasks,
        project_id=project.id,
    )
