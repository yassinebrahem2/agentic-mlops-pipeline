"""Incidents and signals router (I26)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventRecord
from mlops_agents.monitoring.incidents.contract import (
    IncidentStatus,
    IncidentView,
    SignalView,
    WindowSpec,
)
from mlops_agents.monitoring.incidents.maintenance import MaintenanceWindowView
from mlops_agents.tracking.contract import Page

router = APIRouter(tags=["incidents"])


@router.get(
    "/projects/{project_id}/incidents",
    response_model=Page[IncidentView],
)
async def list_incidents(
    project: ProjectDep,
    services: ServicesDep,
    status: Annotated[list[IncidentStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[IncidentView]:
    return await services.incidents.list(project.id, status=status, limit=limit, offset=offset)


@router.get(
    "/projects/{project_id}/incidents/{incident_id}",
    response_model=IncidentView,
)
async def get_incident(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
) -> IncidentView:
    inc = await services.incidents.get(project.id, incident_id)
    if inc is None:
        raise DomainError(
            "incident_not_found",
            f"Incident '{incident_id}' not found.",
            status=404,
        )
    return inc


@router.get(
    "/projects/{project_id}/incidents/{incident_id}/signals",
    response_model=list[SignalView],
)
async def get_incident_signals(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
) -> list[SignalView]:
    return await services.incidents.signals(project.id, incident_id)


@router.get(
    "/projects/{project_id}/incidents/{incident_id}/timeline",
    response_model=list[EventRecord],
)
async def get_incident_timeline(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
    before: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[EventRecord]:
    inc = await services.incidents.get(project.id, incident_id)
    if inc is None:
        raise DomainError(
            "incident_not_found",
            f"Incident '{incident_id}' not found.",
            status=404,
        )
    return await services.events.page(
        project.id, subject_id=incident_id, before=before, limit=limit
    )


@router.post(
    "/projects/{project_id}/incidents/{incident_id}/acknowledge",
    response_model=IncidentView,
)
async def acknowledge_incident(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
) -> IncidentView:
    return await services.incidents.acknowledge(project.id, incident_id, by="user")


@router.post(
    "/projects/{project_id}/incidents/{incident_id}/close",
    response_model=IncidentView,
)
async def close_incident(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
) -> IncidentView:
    return await services.incidents.close(project.id, incident_id, by="user")


class SnoozeIncidentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: datetime


@router.post(
    "/projects/{project_id}/incidents/{incident_id}/snooze",
    response_model=IncidentView,
)
async def snooze_incident(
    project: ProjectDep,
    incident_id: str,
    body: SnoozeIncidentBody,
    services: ServicesDep,
) -> IncidentView:
    return await services.incidents.snooze(project.id, incident_id, until=body.until, by="user")


@router.post(
    "/projects/{project_id}/maintenance-windows",
    status_code=status.HTTP_201_CREATED,
    response_model=MaintenanceWindowView,
)
async def create_maintenance_window(
    project: ProjectDep,
    body: WindowSpec,
    services: ServicesDep,
) -> MaintenanceWindowView:
    return await services.maintenance_windows.create(project.id, body, created_by="user")


@router.get(
    "/projects/{project_id}/maintenance-windows",
    response_model=list[MaintenanceWindowView],
)
async def list_maintenance_windows(
    project: ProjectDep,
    services: ServicesDep,
) -> list[MaintenanceWindowView]:
    return await services.maintenance_windows.list(project.id)


@router.delete(
    "/projects/{project_id}/maintenance-windows/{window_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_maintenance_window(
    project: ProjectDep,
    window_id: str,
    services: ServicesDep,
) -> Response:
    await services.maintenance_windows.delete(project.id, window_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
