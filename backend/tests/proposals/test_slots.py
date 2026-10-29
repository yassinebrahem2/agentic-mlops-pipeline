"""Tests for proposal credential slot state and sweep."""

from __future__ import annotations

import logging

import pytest

from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.proposals.contract import ApplyResult, CredentialSlot
from mlops_agents.proposals.slots import get_slot_states, sweep
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore


class TrackingKeyStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_called_for: list[str] = []
        self.fail_delete_for: set[str] = set()

    def get(self, ref: str) -> str | None:
        self.get_called_for.append(ref)
        return super().get(ref)

    def delete(self, ref: str) -> None:
        if ref in self.fail_delete_for:
            raise KeyStoreError(f"Simulated keystore deletion failure for {ref}")
        super().delete(ref)


@pytest.fixture
def tracking_keystore() -> TrackingKeyStore:
    return TrackingKeyStore()


@pytest.fixture
def project_secrets(tracking_keystore: TrackingKeyStore, databases: Databases) -> ProjectSecrets:
    return ProjectSecrets(tracking_keystore, databases)


@pytest.mark.asyncio
async def test_slot_state_reflects_is_set_without_reading_keystore(
    project_secrets: ProjectSecrets,
    tracking_keystore: TrackingKeyStore,
) -> None:
    project_id = "prj_test"
    proposal_id = "prp_test"
    slots = [
        CredentialSlot(index=0, label="Slot 0", source_type="token"),
        CredentialSlot(index=1, label="Slot 1", source_type="password"),
    ]

    states = await get_slot_states(project_secrets, project_id, proposal_id, slots)
    assert len(states) == 2
    assert states[0].index == 0
    assert states[0].label == "Slot 0"
    assert not states[0].set
    assert states[1].index == 1
    assert not states[1].set
    assert tracking_keystore.get_called_for == []

    # Store a secret for slot 0
    ref_0 = proposal_slot_ref(project_id, proposal_id, 0)
    await project_secrets.put(ref_0, "secret-val-0")

    states = await get_slot_states(project_secrets, project_id, proposal_id, slots)
    assert states[0].set
    assert not states[1].set
    assert tracking_keystore.get_called_for == []


@pytest.mark.asyncio
async def test_sweep_deletes_all_needed_slots(
    project_secrets: ProjectSecrets,
    tracking_keystore: TrackingKeyStore,
) -> None:
    project_id = "prj_test"
    proposal_id = "prp_test"
    slots = [
        CredentialSlot(index=0, label="Slot 0", source_type="token"),
        CredentialSlot(index=1, label="Slot 1", source_type="password"),
    ]

    ref_0 = proposal_slot_ref(project_id, proposal_id, 0)
    ref_1 = proposal_slot_ref(project_id, proposal_id, 1)
    await project_secrets.put(ref_0, "val0")
    await project_secrets.put(ref_1, "val1")

    assert await project_secrets.is_set(ref_0)
    assert await project_secrets.is_set(ref_1)

    await sweep(project_secrets, project_id, proposal_id, slots)

    assert not await project_secrets.is_set(ref_0)
    assert not await project_secrets.is_set(ref_1)
    assert tracking_keystore.get(ref_0) is None
    assert tracking_keystore.get(ref_1) is None


@pytest.mark.asyncio
async def test_sweep_keystore_error_is_logged_and_added_to_result(
    project_secrets: ProjectSecrets,
    tracking_keystore: TrackingKeyStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id = "prj_test"
    proposal_id = "prp_test"
    slots = [
        CredentialSlot(index=0, label="Slot 0", source_type="token"),
        CredentialSlot(index=1, label="Slot 1", source_type="password"),
    ]

    ref_0 = proposal_slot_ref(project_id, proposal_id, 0)
    ref_1 = proposal_slot_ref(project_id, proposal_id, 1)
    await project_secrets.put(ref_0, "val0")
    await project_secrets.put(ref_1, "val1")

    tracking_keystore.fail_delete_for.add(ref_0)

    result = ApplyResult(applied=["step1"], failed={})
    with caplog.at_level(logging.ERROR):
        await sweep(project_secrets, project_id, proposal_id, slots, result=result)

    assert "credential 0" in result.failed
    assert "Simulated keystore deletion failure" in result.failed["credential 0"]
    assert "credential 1" not in result.failed
    # slot 1 was still deleted despite slot 0 failure
    assert not await project_secrets.is_set(ref_1)
