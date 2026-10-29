"""Proposals router: slot route, by-approval route, and handoff route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import HandoffView, ProposalView, SlotState

router = APIRouter(tags=["proposals"])


class CredentialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Annotated[SecretStr, Field(min_length=1, max_length=8192)]


@router.put(
    "/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}",
    response_model=SlotState,
)
async def put_credential_slot(
    project: ProjectDep,
    proposal_id: str,
    slot: int,
    body: CredentialBody,
    services: ServicesDep,
) -> SlotState:
    return await services.proposals.put_credential(
        project.id, proposal_id, slot, body.value.get_secret_value()
    )


@router.get(
    "/projects/{project_id}/proposals/by-approval/{approval_id}",
    response_model=ProposalView,
)
async def get_proposal_by_approval(
    project: ProjectDep,
    approval_id: str,
    services: ServicesDep,
) -> ProposalView:
    view = await services.proposals.get_by_approval(project.id, approval_id)
    if view is None:
        raise DomainError(
            "proposal_not_found",
            f"No proposal found for approval '{approval_id}'.",
            status=404,
        )
    return view


@router.get(
    "/projects/{project_id}/handoff",
    response_model=HandoffView | None,
)
async def get_current_handoff(
    project: ProjectDep,
    services: ServicesDep,
) -> HandoffView | None:
    return await services.handoffs.current(project.id)
