"""swapped_kind: a fake under a real kind's name for one block, the real kind back after."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.proposals.contract import CONNECTIONS_KIND, ApplyResult, CredentialSlot
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ConnectionsPayload
from tests.fakes.proposal_kinds import swapped_kind


class _Kind:
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(self, label: str) -> None:
        self.name = CONNECTIONS_KIND
        self.label = label

    def key(self, payload: BaseModel) -> str:
        return "key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title=self.label, body="")

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        return ApplyResult(applied=[], failed={})


def _registry() -> tuple[ProposalKinds, _Kind]:
    kinds = ProposalKinds()
    real = _Kind("real")
    kinds.register(real)
    return kinds, real


def test_the_fake_serves_inside_the_block_and_the_real_kind_returns() -> None:
    kinds, real = _registry()
    fake = _Kind("fake")

    with swapped_kind(kinds, fake):
        assert kinds.get(CONNECTIONS_KIND) is fake

    assert kinds.get(CONNECTIONS_KIND) is real


def test_the_real_kind_returns_after_an_exception() -> None:
    kinds, real = _registry()

    with pytest.raises(RuntimeError), swapped_kind(kinds, _Kind("fake")):
        raise RuntimeError("boom")

    assert kinds.get(CONNECTIONS_KIND) is real


def test_a_kind_with_no_real_one_is_removed_on_exit() -> None:
    kinds = ProposalKinds()
    fake = _Kind("fake")

    with swapped_kind(kinds, fake):
        assert kinds.get(CONNECTIONS_KIND) is fake

    kinds.register(_Kind("later"))  # the name is free again


def test_registering_a_second_real_kind_still_raises() -> None:
    kinds, _ = _registry()

    with pytest.raises(ValueError, match="already registered"):
        kinds.register(_Kind("second"))
