"""Training router: approve hooks (Task 7) and training policies (Task 8)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import HookView
from mlops_agents.training.contract import TrainingPolicyView

router = APIRouter(tags=["training"])


class SetPolicyEnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.post(
    "/projects/{project_id}/hooks/{hook_id}/approve",
    response_model=HookView,
)
async def approve_hook(
    project: ProjectDep,
    hook_id: str,
    services: ServicesDep,
) -> HookView:
    try:
        hook = await services.hooks.approve(project.id, hook_id, by="user")
        await services.training.activate_policies_for_hook(project.id, hook_id)
        return hook
    except KeyError:
        raise DomainError("hook_not_found", f"Hook '{hook_id}' not found.", status=404) from None


@router.get(
    "/projects/{project_id}/training/policies",
    response_model=list[TrainingPolicyView],
)
async def list_training_policies(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = None,
) -> list[TrainingPolicyView]:
    return await services.training.policies(project.id, model=model)


@router.put(
    "/projects/{project_id}/training/policies/{policy_id}/enabled",
    response_model=TrainingPolicyView,
)
async def set_policy_enabled(
    project: ProjectDep,
    policy_id: str,
    body: SetPolicyEnabledBody,
    services: ServicesDep,
) -> TrainingPolicyView:
    try:
        return await services.training.set_policy_enabled(project.id, policy_id, body.enabled)
    except KeyError:
        raise DomainError(
            "policy_not_found", f"Policy '{policy_id}' not found.", status=404
        ) from None
