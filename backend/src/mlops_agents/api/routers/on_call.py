"""On-call schedule router."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.notifications.constants import contact_problems
from mlops_agents.notifications.contract import OnCallPerson
from mlops_agents.proposals.payloads import OnCall

router = APIRouter(prefix="/projects/{project_id}/on-call", tags=["on_call"])


@router.get(
    "",
    response_model=list[OnCallPerson],
)
async def list_on_call(
    project: ProjectDep,
    services: ServicesDep,
) -> list[OnCallPerson]:
    return await services.on_call.list(project.id)


@router.put(
    "",
    response_model=list[OnCallPerson],
)
async def replace_on_call(
    project: ProjectDep,
    body: list[OnCall],
    services: ServicesDep,
) -> list[OnCallPerson]:
    if problems := contact_problems(body):
        raise DomainError("invalid_contact", " ".join(problems), status=422)
    return await services.on_call.replace(project.id, body)
