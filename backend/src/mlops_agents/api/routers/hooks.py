"""Action hooks router: list hooks, credential management, and enable/disable."""

from __future__ import annotations

import unicodedata

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import HookView

router = APIRouter(tags=["hooks"])


class SetHookCredentialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: str = Field(min_length=1)

    @field_validator("credential")
    @classmethod
    def _no_control_characters(cls, v: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c).startswith("C") for c in v):
            raise ValueError("Credential must not contain control characters or newlines")
        return v


class SetEnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get(
    "/projects/{project_id}/hooks",
    response_model=list[HookView],
)
async def list_hooks(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = None,
) -> list[HookView]:
    return await services.hooks.list(project.id, model=model)


@router.put(
    "/projects/{project_id}/hooks/{hook_id}/credential",
    response_model=HookView,
)
async def set_hook_credential(
    project: ProjectDep,
    hook_id: str,
    body: SetHookCredentialBody,
    services: ServicesDep,
) -> HookView:
    try:
        return await services.hooks.set_credential(project.id, hook_id, body.credential)
    except KeyError:
        raise DomainError("hook_not_found", f"Hook '{hook_id}' not found.", status=404) from None


@router.delete(
    "/projects/{project_id}/hooks/{hook_id}/credential",
    response_model=HookView,
)
async def delete_hook_credential(
    project: ProjectDep,
    hook_id: str,
    services: ServicesDep,
) -> HookView:
    try:
        return await services.hooks.delete_credential(project.id, hook_id)
    except KeyError:
        raise DomainError("hook_not_found", f"Hook '{hook_id}' not found.", status=404) from None


@router.put(
    "/projects/{project_id}/hooks/{hook_id}/enabled",
    response_model=HookView,
)
async def set_hook_enabled(
    project: ProjectDep,
    hook_id: str,
    body: SetEnabledBody,
    services: ServicesDep,
) -> HookView:
    try:
        return await services.hooks.set_enabled(project.id, hook_id, body.enabled)
    except KeyError:
        raise DomainError("hook_not_found", f"Hook '{hook_id}' not found.", status=404) from None
