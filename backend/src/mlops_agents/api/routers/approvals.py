"""Approvals and permission settings router."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.settings import get_permission_level, set_permission_level
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.api.deps import ProjectDep, ServicesDep

router = APIRouter(tags=["approvals"])


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str | None = None


class PermissionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: PermissionLevel


@router.get(
    "/projects/{project_id}/approvals",
    response_model=list[ApprovalView],
)
async def list_pending_approvals(
    project: ProjectDep,
    services: ServicesDep,
) -> list[ApprovalView]:
    return await services.approvals.pending(project.id)


@router.post(
    "/projects/{project_id}/approvals/{approval_id}/decision",
    response_model=ApprovalView,
)
async def decide_approval(
    project: ProjectDep,
    approval_id: str,
    body: DecisionRequest,
    services: ServicesDep,
) -> ApprovalView:
    return await services.approvals.decide(
        project.id, approval_id, approve=body.approve, note=body.note
    )


@router.get(
    "/projects/{project_id}/settings/permissions",
    response_model=PermissionsSettings,
)
async def get_permissions_settings(
    project: ProjectDep,
    services: ServicesDep,
) -> PermissionsSettings:
    level = await get_permission_level(services.databases, project.id)
    return PermissionsSettings(level=level)


@router.put(
    "/projects/{project_id}/settings/permissions",
    response_model=PermissionsSettings,
)
async def update_permissions_settings(
    project: ProjectDep,
    body: PermissionsSettings,
    services: ServicesDep,
) -> PermissionsSettings:
    level = await set_permission_level(services.databases, project.id, body.level)
    return PermissionsSettings(level=level)
