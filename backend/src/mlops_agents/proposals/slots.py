"""Slot state and sweep for proposal credentials."""

from __future__ import annotations

import logging

from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.credentials.scrub import error_text
from mlops_agents.proposals.contract import ApplyResult, CredentialSlot, SlotState

logger = logging.getLogger(__name__)


async def get_slot_states(
    project_secrets: ProjectSecrets,
    project_id: str,
    proposal_id: str,
    needed: list[CredentialSlot],
) -> list[SlotState]:
    """Computes the SlotState for each needed slot based on secret_refs (no keystore read)."""
    states: list[SlotState] = []
    for slot in needed:
        ref = proposal_slot_ref(project_id, proposal_id, slot.index)
        is_set = await project_secrets.is_set(ref)
        states.append(SlotState(index=slot.index, label=slot.label, set=is_set))
    return states


async def sweep(
    project_secrets: ProjectSecrets,
    project_id: str,
    proposal_id: str,
    needed: list[CredentialSlot],
    result: ApplyResult | None = None,
) -> None:
    """Deletes all credential slots for a proposal.

    Serves reject, supersede, cancel, expiry, failed apply and leftovers after a partial apply.
    A KeyStoreError is logged and added to result.failed, never silent.
    """
    for slot in needed:
        ref = proposal_slot_ref(project_id, proposal_id, slot.index)
        try:
            await project_secrets.delete(ref)
        except KeyStoreError as exc:
            logger.exception(
                "KeyStoreError while deleting slot %s for proposal %s",
                slot.index,
                proposal_id,
            )
            if result is not None:
                result.failed[f"credential {slot.index}"] = error_text(exc)
