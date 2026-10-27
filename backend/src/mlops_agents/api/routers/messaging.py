"""Messaging settings routes."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.agents.messaging.settings import MessagingLimits
from mlops_agents.api.deps import ProjectDep, ServicesDep

router = APIRouter(tags=["messaging"])


@router.get("/projects/{project_id}/settings/messaging", response_model=MessagingLimits)
async def get_messaging_settings(project: ProjectDep, services: ServicesDep) -> MessagingLimits:
    return await services.messaging_settings.get(project.id)


@router.put("/projects/{project_id}/settings/messaging", response_model=MessagingLimits)
async def set_messaging_settings(
    project: ProjectDep, body: MessagingLimits, services: ServicesDep
) -> MessagingLimits:
    return await services.messaging_settings.set(project.id, body)
