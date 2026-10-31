"""Dashboard route (I39)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.contract import DashboardView

router = APIRouter(tags=["dashboard"])


@router.get("/projects/{project_id}/dashboard", response_model=DashboardView)
async def get_dashboard(project: ProjectDep, services: ServicesDep) -> DashboardView:
    return await services.dashboard.build(project.id)
