"""The `promotion` proposal kind and check (Task 3, #497)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.promotion.contract import PromotionPayload
from mlops_agents.promotion.promotions import SqlitePromotions
from mlops_agents.proposals.contract import (
    PROMOTION_KIND,
    ApplyResult,
    CredentialSlot,
    ProposalCheck,
)

__all__ = ["PromotionKind"]


def _payload(payload: BaseModel | dict[str, Any]) -> PromotionPayload:
    if isinstance(payload, PromotionPayload):
        return payload
    return PromotionPayload.model_validate(payload)


class PromotionKind(ProposalCheck):
    """ProposalKind and ProposalCheck implementation for promotions."""

    name = PROMOTION_KIND
    payload_model: type[BaseModel] = PromotionPayload

    def __init__(self, promotions: SqlitePromotions) -> None:
        self._promotions = promotions

    def key(self, payload: BaseModel) -> str:
        return _payload(payload).model

    def preview(self, payload: BaseModel) -> Preview:
        p = _payload(payload)
        champ = f" (replacing champion {p.champion_version})" if p.champion_version else ""
        return Preview(
            kind="text",
            title=f"Promote {p.model} {p.candidate_version}",
            body=(
                f"Promote candidate version {p.candidate_version} of model {p.model}{champ} "
                f"using deploy hook {p.deploy_hook_id}."
            ),
        )

    def warnings(self, payload: BaseModel) -> list[str]:
        return ["Approving runs the deploy hook."]

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def check(self, project_id: str, payload: Any) -> list[str]:
        """Refusals returned at propose time before the gate."""
        p = _payload(payload)
        return await self._promotions.validate(project_id, p)

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        """Idempotent application of the approved promotion proposal."""
        p = _payload(payload)
        return await self._promotions.apply(project_id, proposal_id, p)
