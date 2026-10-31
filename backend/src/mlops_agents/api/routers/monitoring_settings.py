"""Monitoring settings router."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.monitoring.settings import (
    MonitoringSettings,
    get_monitoring_settings,
    set_monitoring_settings,
)

router = APIRouter(tags=["monitoring_settings"])


@router.get(
    "/projects/{project_id}/monitoring-settings",
    response_model=MonitoringSettings,
)
async def get_project_monitoring_settings(
    project: ProjectDep,
    services: ServicesDep,
) -> MonitoringSettings:
    return await get_monitoring_settings(services.databases, project.id)


@router.put(
    "/projects/{project_id}/monitoring-settings",
    response_model=MonitoringSettings,
)
async def update_project_monitoring_settings(
    project: ProjectDep,
    body: MonitoringSettings,
    services: ServicesDep,
) -> MonitoringSettings:
    return await set_monitoring_settings(services.databases, project.id, body)
