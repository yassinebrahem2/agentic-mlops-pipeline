"""Agent catalog and agent settings routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.agents.catalog import AgentDefinition
from mlops_agents.agents.settings import AgentLimitsUpdate, AgentSettingsView
from mlops_agents.api.deps import ProjectDep, ServicesDep

router = APIRouter(tags=["agents"])


class SetAgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str | None


class AgentInfo(BaseModel):
    name: str
    display_name: str
    description: str
    chat: bool
    tools: list[str]
    settings: AgentSettingsView


def _to_agent_info(definition: AgentDefinition, settings: AgentSettingsView) -> AgentInfo:
    return AgentInfo(
        name=definition.name,
        display_name=definition.display_name,
        description=definition.description,
        chat=definition.chat,
        tools=list(definition.tools),
        settings=settings,
    )


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents(services: ServicesDep) -> list[AgentInfo]:
    definitions = sorted(services.catalog.all(), key=lambda d: d.name)
    return [_to_agent_info(d, await services.agent_settings.get(d.name)) for d in definitions]


@router.put("/agents/{agent}/model", response_model=AgentInfo)
async def set_agent_model(agent: str, body: SetAgentModel, services: ServicesDep) -> AgentInfo:
    definition = services.catalog.get(agent)
    settings = await services.agent_settings.set_default_model(agent, body.address)
    return _to_agent_info(definition, settings)


@router.get("/projects/{project_id}/agents", response_model=list[AgentInfo])
async def list_project_agents(project: ProjectDep, services: ServicesDep) -> list[AgentInfo]:
    definitions = sorted(services.catalog.all(), key=lambda d: d.name)
    return [
        _to_agent_info(d, await services.agent_settings.get(d.name, project_id=project.id))
        for d in definitions
    ]


@router.put("/projects/{project_id}/agents/{agent}/model", response_model=AgentInfo)
async def set_project_agent_model(
    agent: str, project: ProjectDep, body: SetAgentModel, services: ServicesDep
) -> AgentInfo:
    definition = services.catalog.get(agent)
    settings = await services.agent_settings.set_project_model(project.id, agent, body.address)
    return _to_agent_info(definition, settings)


@router.put("/projects/{project_id}/agents/{agent}/limits", response_model=AgentInfo)
async def set_project_agent_limits(
    agent: str, project: ProjectDep, body: AgentLimitsUpdate, services: ServicesDep
) -> AgentInfo:
    definition = services.catalog.get(agent)
    settings = await services.agent_settings.set_project_limits(project.id, agent, body)
    return _to_agent_info(definition, settings)
