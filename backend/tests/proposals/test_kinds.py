"""Tests for ProposalKinds registry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    HANDOFF_KIND,
    ApplyResult,
    CredentialSlot,
)
from mlops_agents.proposals.kinds import ProposalKinds


class DummyPayload(BaseModel):
    pass


class DummyKind:
    payload_model: type[BaseModel] = DummyPayload

    def __init__(self, name: str) -> None:
        self.name = name

    def key(self, payload: BaseModel) -> str:
        return "key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Dummy", body="dummy")

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        return ApplyResult(applied=[], failed={})


def test_proposal_kinds_register_and_get() -> None:
    kinds = ProposalKinds()
    kind = DummyKind("test_kind")
    kinds.register(kind)
    assert kinds.get("test_kind") is kind


def test_proposal_kinds_duplicate_registration_raises() -> None:
    kinds = ProposalKinds()
    kind = DummyKind("dup")
    kinds.register(kind)
    with pytest.raises(ValueError, match="already registered"):
        kinds.register(DummyKind("dup"))


def test_proposal_kinds_unknown_name_is_422() -> None:
    kinds = ProposalKinds()
    with pytest.raises(DomainError) as exc_info:
        kinds.get("nonexistent")
    assert exc_info.value.status == 422
    assert exc_info.value.code == "unknown_proposal_kind"


def test_proposal_kinds_checked_against_constants() -> None:
    kinds = ProposalKinds()
    k1 = DummyKind(CONNECTIONS_KIND)
    k2 = DummyKind(HANDOFF_KIND)
    kinds.register(k1)
    kinds.register(k2)
    assert kinds.get(CONNECTIONS_KIND) is k1
    assert kinds.get(HANDOFF_KIND) is k2
