"""Monitoring rules router: list rules and enable/disable (I23)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.rules.contract import RuleView

router = APIRouter(tags=["rules"])


class SetEnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get(
    "/projects/{project_id}/rules",
    response_model=list[RuleView],
)
async def list_rules(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = None,
) -> list[RuleView]:
    return await services.rules.list(project.id, model=model)


@router.put(
    "/projects/{project_id}/rules/{rule_id}/enabled",
    response_model=RuleView,
)
async def set_rule_enabled(
    project: ProjectDep,
    rule_id: str,
    body: SetEnabledBody,
    services: ServicesDep,
) -> RuleView:
    try:
        return await services.rules.set_enabled(project.id, rule_id, body.enabled)
    except KeyError:
        raise DomainError("rule_not_found", f"Rule '{rule_id}' not found.", status=404) from None
