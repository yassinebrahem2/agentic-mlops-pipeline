"""Projects and the folder browser."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.workspace.models import FolderListing, Project

router = APIRouter(tags=["projects"])


class AddLocalProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["local"]
    path: str
    init_git: bool = False


class AddGitHubProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["github"]
    repo: str


AddProject = Annotated[AddLocalProject | AddGitHubProject, Field(discriminator="source")]


class RelinkProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


@router.get("/projects", response_model=list[Project])
async def list_projects(services: ServicesDep) -> list[Project]:
    return await services.workspace.list_projects()


@router.post("/projects", status_code=201, response_model=Project)
async def add_project(body: AddProject, services: ServicesDep) -> Project:
    if body.source == "local":
        return await services.workspace.add_local(body.path, init_git=body.init_git)
    return await services.workspace.add_github(body.repo)


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project: ProjectDep) -> Project:
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project: ProjectDep, services: ServicesDep) -> None:
    await services.sessions.close_project(project.id)
    await services.events.close_project(project.id)
    await services.workspace.remove(project.id)


@router.post("/projects/{project_id}/open", response_model=Project)
async def open_project(project: ProjectDep, services: ServicesDep) -> Project:
    return await services.workspace.open(project.id)


@router.post("/projects/{project_id}/refresh", response_model=Project)
async def refresh_project(project: ProjectDep, services: ServicesDep) -> Project:
    return await services.workspace.refresh(project.id)


@router.post("/projects/{project_id}/relink", response_model=Project)
async def relink_project(
    body: RelinkProject, project: ProjectDep, services: ServicesDep
) -> Project:
    return await services.workspace.relink(project.id, body.path)


@router.get("/folders", response_model=FolderListing)
async def browse_folders(services: ServicesDep, path: str | None = None) -> FolderListing:
    return await services.workspace.browse(path)
