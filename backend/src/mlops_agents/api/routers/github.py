"""GitHub integration routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.integrations.github.api import RepoSummary
from mlops_agents.integrations.github.auth import GitHubStatus

router = APIRouter(prefix="/github", tags=["github"])


class SetGitHubToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(repr=False)


@router.get("", response_model=GitHubStatus)
async def get_github(services: ServicesDep) -> GitHubStatus:
    return await services.github.status()


@router.post("/gh", response_model=GitHubStatus)
async def enable_gh(services: ServicesDep) -> GitHubStatus:
    return await services.github.enable_gh()


@router.post("/token", response_model=GitHubStatus)
async def set_token(body: SetGitHubToken, services: ServicesDep) -> GitHubStatus:
    return await services.github.set_token(body.token)


@router.delete("", status_code=204)
async def disconnect_github(services: ServicesDep) -> None:
    await services.github.disconnect()


@router.get("/repos", response_model=list[RepoSummary])
async def list_repos(services: ServicesDep) -> list[RepoSummary]:
    return await services.github.list_repos()
