"""The `connections` proposal kind: each approved entry becomes one data connection."""

from __future__ import annotations

from functools import partial

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.connections.contract import Connections
from mlops_agents.errors import DomainError
from mlops_agents.onboarding.kinds_common import PROJECT_KEY, Parts, SecretValues
from mlops_agents.proposals.contract import CONNECTIONS_KIND, ApplyResult, CredentialSlot
from mlops_agents.proposals.payloads import ConnectionsPayload, ConnectionSpec


def _payload(payload: BaseModel) -> ConnectionsPayload:
    if not isinstance(payload, ConnectionsPayload):
        raise TypeError(f"the connections kind got a {type(payload).__name__}")
    return payload


class ConnectionsKind:
    name = CONNECTIONS_KIND
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(self, connections: Connections, secret_values: SecretValues) -> None:
        self._connections = connections
        self._secret_values = secret_values

    def key(self, payload: BaseModel) -> str:
        return PROJECT_KEY

    def preview(self, payload: BaseModel) -> Preview:
        """Pure: the denylist findings come from the card's own inspect call."""
        lines = [
            f"{spec.name} ({spec.kind}): path {spec.settings.path}\n  why: {spec.why}"
            for spec in _payload(payload).connections
        ]
        return Preview(kind="text", title="Connections", body="\n".join(lines))

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        parts = Parts(self._secret_values)
        for spec in _payload(payload).connections:
            await parts.run(f"connection:{spec.name}", partial(self._create, project_id, spec))
        return parts.result()

    async def _create(self, project_id: str, spec: ConnectionSpec) -> None:
        """Idempotent: a connection of that name, kind and settings counts as applied."""
        settings = spec.settings.model_dump(mode="json")
        current = await self._connections.list(project_id)
        existing = next((c for c in current if c.name == spec.name), None)
        if existing is None:
            await self._connections.create(project_id, spec.kind, spec.name, settings)
        elif existing.kind != spec.kind or existing.settings != settings:
            raise DomainError(
                "connection_name_taken",
                f"a connection named {spec.name} already exists with other settings",
                status=409,
            )
