"""Embedder settings routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.providers.embedder import EmbedderStatus

router = APIRouter(tags=["embedders"])


class EmbedderChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str | None


class EmbedderSettingView(BaseModel):
    chosen: str | None
    status: EmbedderStatus


@router.get("/settings/embedder", response_model=EmbedderSettingView)
async def get_app_embedder(services: ServicesDep) -> EmbedderSettingView:
    chosen = await services.embedders.chosen_default()
    status = await services.embedders.default_status()
    return EmbedderSettingView(chosen=chosen, status=status)


@router.put("/settings/embedder", response_model=EmbedderSettingView)
async def set_app_embedder(body: EmbedderChoice, services: ServicesDep) -> EmbedderSettingView:
    await services.embedders.set_default(body.address)
    chosen = await services.embedders.chosen_default()
    status = await services.embedders.default_status()
    return EmbedderSettingView(chosen=chosen, status=status)


@router.get("/projects/{project_id}/settings/embedder", response_model=EmbedderSettingView)
async def get_project_embedder(project: ProjectDep, services: ServicesDep) -> EmbedderSettingView:
    chosen = await services.embedders.chosen_override(project.id)
    status = await services.embedders.status(project.id)
    return EmbedderSettingView(chosen=chosen, status=status)


@router.put("/projects/{project_id}/settings/embedder", response_model=EmbedderSettingView)
async def set_project_embedder(
    project: ProjectDep, body: EmbedderChoice, services: ServicesDep
) -> EmbedderSettingView:
    await services.embedders.set_override(project.id, body.address)
    chosen = await services.embedders.chosen_override(project.id)
    status = await services.embedders.status(project.id)
    return EmbedderSettingView(chosen=chosen, status=status)
