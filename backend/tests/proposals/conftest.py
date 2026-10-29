"""Shared fixtures and scripted proposal kinds for proposals service tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.proposals.contract import (
    HANDOFF_KIND,
    ApplyResult,
    CredentialSlot,
    HandoffPayload,
)
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore


class ScriptedTestKind:
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(
        self,
        name: str = "connections",
        slots: list[CredentialSlot] | None = None,
        warnings_list: list[str] | None = None,
    ) -> None:
        self.name = name
        self._slots = (
            slots
            if slots is not None
            else [
                CredentialSlot(index=0, label="Key 0", source_type="token"),
                CredentialSlot(index=1, label="Key 1", source_type="password"),
            ]
        )
        self._warnings = warnings_list or ["Test warning line 1"]
        self.apply_calls: list[tuple[str, str, Any]] = []
        self.apply_error: Exception | None = None
        self.apply_result: ApplyResult | None = None
        self.on_apply_coro: Any = None

    def key(self, payload: BaseModel) -> str:
        return "const_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Test Preview", body="preview text")

    def warnings(self, payload: BaseModel) -> list[str]:
        return list(self._warnings)

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return list(self._slots)

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append((project_id, proposal_id, payload))
        if self.on_apply_coro is not None:
            return await self.on_apply_coro(project_id, proposal_id, payload)
        if self.apply_error is not None:
            raise self.apply_error
        if self.apply_result is not None:
            return self.apply_result
        return ApplyResult(applied=["step_applied"], failed={})


class HandoffTestKind:
    payload_model: type[BaseModel] = HandoffPayload

    def __init__(self, handoffs: SqliteHandoffs) -> None:
        self.name = HANDOFF_KIND
        self._handoffs = handoffs
        self.apply_calls: list[str] = []

    def key(self, payload: BaseModel) -> str:
        return "handoff_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Handoff", body="handoff preview")

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append(proposal_id)
        assert isinstance(payload, HandoffPayload)
        v = await self._handoffs.store(project_id, proposal_id, payload)
        return ApplyResult(applied=[f"version_{v}"], failed={})


@pytest.fixture
def memory_keystore() -> MemoryKeyStore:
    return MemoryKeyStore()


@pytest.fixture
def project_secrets(memory_keystore: MemoryKeyStore, databases: Databases) -> ProjectSecrets:
    return ProjectSecrets(memory_keystore, databases)


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def kinds() -> ProposalKinds:
    return ProposalKinds()


@pytest.fixture
def service(
    databases: Databases,
    project_secrets: ProjectSecrets,
    kinds: ProposalKinds,
    event_log: SqliteEventLog,
) -> ProposalsService:
    return ProposalsService(databases, project_secrets, kinds, event_log)
