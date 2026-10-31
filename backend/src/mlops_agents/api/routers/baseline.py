"""Baseline router (I31): an incident's team-alert links, the silent team alerts, their verdicts."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.verdicts.contract import BaselineLink, SilentTeamAlert, SilentVerdict

router = APIRouter(tags=["baseline"])


class SilentVerdictBody(BaseModel):
    """A person's verdict on a team alert no incident matched."""

    model_config = ConfigDict(extra="forbid")

    value: SilentVerdict


@router.get(
    "/projects/{project_id}/incidents/{incident_id}/baseline",
    response_model=list[BaselineLink],
)
async def list_baseline_links(
    project: ProjectDep,
    incident_id: str,
    services: ServicesDep,
) -> list[BaselineLink]:
    if await services.incidents.get(project.id, incident_id) is None:
        raise DomainError("incident_not_found", f"Incident '{incident_id}' not found.", status=404)
    return await services.baseline.links(project.id, incident_id)


@router.get(
    "/projects/{project_id}/baseline/silent-alerts",
    response_model=list[SilentTeamAlert],
)
async def list_silent_alerts(project: ProjectDep, services: ServicesDep) -> list[SilentTeamAlert]:
    return await services.baseline.silent_alerts(project.id)


@router.post(
    "/projects/{project_id}/baseline/silent-alerts/{result_id}/verdict",
    response_model=SilentTeamAlert,
)
async def judge_silent_alert(
    project: ProjectDep,
    result_id: str,
    body: SilentVerdictBody,
    services: ServicesDep,
) -> SilentTeamAlert:
    return await services.baseline.judge_silent(project.id, result_id, body.value, by="user")
