"""Stats router (I41): how the agents do against the team's own monitoring, computed on read."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.stats.contract import StatsView, StatsWindow

router = APIRouter(tags=["stats"])


@router.get("/projects/{project_id}/stats", response_model=StatsView)
async def get_stats(
    project: ProjectDep,
    services: ServicesDep,
    window: StatsWindow = "30d",
    category: ResultCategory | None = None,
) -> StatsView:
    return await services.stats.compute(project.id, window, category, services.clock.now())
