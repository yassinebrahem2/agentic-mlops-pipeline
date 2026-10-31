"""Expectations router (I25)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.expectations.contract import ExpectationState

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
