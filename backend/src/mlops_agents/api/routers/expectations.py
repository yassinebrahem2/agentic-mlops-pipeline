"""Expectations router (I25)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.expectations.contract import CheckView, ExpectationState

router = APIRouter(tags=["expectations"])


@router.get(
    "/projects/{project_id}/expectations",
    response_model=list[ExpectationState],
)
async def list_expectations(
    project: ProjectDep,
    services: ServicesDep,
) -> list[ExpectationState]:
    return await services.expectations.states(project.id)


@router.get(
    "/projects/{project_id}/checks",
    response_model=list[CheckView],
)
async def list_checks(
    project: ProjectDep,
    services: ServicesDep,
) -> list[CheckView]:
    return await services.checks.list(project.id)
