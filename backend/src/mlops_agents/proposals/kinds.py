"""The ProposalKinds registry: register and get proposal kinds."""

from __future__ import annotations

from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import ProposalKind


class ProposalKinds:
    """Registry for ProposalKind instances."""

    def __init__(self) -> None:
        self._kinds: dict[str, ProposalKind] = {}

    def register(self, kind: ProposalKind) -> None:
        if kind.name in self._kinds:
            raise ValueError(f"Proposal kind {kind.name!r} is already registered.")
        self._kinds[kind.name] = kind

    def get(self, name: str) -> ProposalKind:
        kind = self._kinds.get(name)
        if kind is None:
            raise DomainError(
                "unknown_proposal_kind",
                f"Unknown proposal kind {name!r}.",
                status=422,
            )
        return kind
