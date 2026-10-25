"""Sandbox and Docker status routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.sandbox.contract import DockerStatus, EnvironmentStatus

router = APIRouter(tags=["sandbox"])


class SandboxView(BaseModel):
    docker: DockerStatus
    environment: EnvironmentStatus
    network: bool


class SandboxSettings(BaseModel):
    network: bool


@router.get("/projects/{project_id}/sandbox", response_model=SandboxView)
async def get_project_sandbox(project: ProjectDep, services: ServicesDep) -> SandboxView:
    docker_status = await services.sandbox.docker()
    env_status = await services.sandbox.environment(project.id)
    network = await services.sandbox.get_network(project.id)
    return SandboxView(
        docker=docker_status,
        environment=env_status,
        network=network,
    )


@router.put("/projects/{project_id}/settings/sandbox", response_model=SandboxSettings)
async def update_project_sandbox_settings(
    project: ProjectDep,
    body: SandboxSettings,
    services: ServicesDep,
) -> SandboxSettings:
    network = await services.sandbox.set_network(project.id, body.network)
    return SandboxSettings(network=network)


@router.post("/projects/{project_id}/sandbox/rebuild", response_model=EnvironmentStatus)
async def rebuild_project_sandbox(project: ProjectDep, services: ServicesDep) -> EnvironmentStatus:
    return await services.sandbox.rebuild(project.id)
