"""Training router: approve hooks (Task 7) and training policies (Task 8)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import HookView

router = APIRouter(tags=["training"])


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
        return await services.hooks.approve(project.id, hook_id, by="user")
    except KeyError:
        raise DomainError("hook_not_found", f"Hook '{hook_id}' not found.", status=404) from None
