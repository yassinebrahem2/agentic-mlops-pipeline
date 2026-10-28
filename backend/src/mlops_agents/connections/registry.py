"""Registry for connection kinds."""

from __future__ import annotations

from collections.abc import Iterable

from mlops_agents.connections.contract import ConnectionKind
from mlops_agents.errors import DomainError


class KindRegistry:
    """Maintains available connection kinds by unique name."""

    def __init__(self, kinds: Iterable[ConnectionKind] = ()) -> None:
        self._kinds: dict[str, ConnectionKind] = {k.name: k for k in kinds}

    def register(self, kind: ConnectionKind) -> None:
        self._kinds[kind.name] = kind

    def get(self, name: str) -> ConnectionKind:
        kind = self._kinds.get(name)
        if kind is None:
            raise DomainError(
                "unknown_connection_kind",
                f"Unknown connection kind '{name}'.",
                status=400,
            )
        return kind

    def names(self) -> list[str]:
        return sorted(self._kinds.keys())
