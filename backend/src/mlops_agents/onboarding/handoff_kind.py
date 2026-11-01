"""The `handoff` proposal kind: its card's preview, warnings and slots, and its apply."""

from __future__ import annotations

from functools import partial
from typing import Final

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.connections.contract import Connections
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.contract import EventLog
from mlops_agents.notifications.constants import contact_problems
from mlops_agents.onboarding import handoff_parts as part
from mlops_agents.onboarding.handoff_parts import KickoffStarter, NetworkSetting
from mlops_agents.onboarding.handoff_preview import render
from mlops_agents.onboarding.kinds_common import PROJECT_KEY, Parts, SecretValues
from mlops_agents.proposals.contract import HANDOFF_KIND, ApplyResult, CredentialSlot, Handoffs
from mlops_agents.proposals.payloads import HandoffPayload, TrackingSourceSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import MlflowSettings, Tracking

# The one copy of this sentence: the proposals card shows it as given.
NETWORK_WARNING: Final = (
    "This grants network access while an MLflow tracker accepts changes without "
    "authentication: agents at the autonomous and bypass levels can change it, and every "
    "Bash command they run is recorded in the audit log."
)
KICKOFF_NOT_STARTED: Final = "not started: the handoff was not stored"


def _payload(payload: BaseModel) -> HandoffPayload:
    if not isinstance(payload, HandoffPayload):
        raise TypeError(f"the handoff kind got a {type(payload).__name__}")
    return payload


def _slot_label(spec: TrackingSourceSpec) -> str:
    settings = spec.settings
    where = settings.url if isinstance(settings, MlflowSettings) else settings.repository
    return f"{spec.type} credential for {where}"


class HandoffKind:
    name = HANDOFF_KIND
    payload_model: type[BaseModel] = HandoffPayload

    def __init__(
        self,
        *,
        databases: Databases,
        connections: Connections,
        tracking: Tracking,
        handoffs: Handoffs,
        sandbox: NetworkSetting,
        events: EventLog,
        kickoff: KickoffStarter,
        secrets: ProjectSecrets,
        secret_values: SecretValues,
    ) -> None:
        self._databases = databases
        self._connections = connections
        self._tracking = tracking
        self._handoffs = handoffs
        self._sandbox = sandbox
        self._events = events
        self._kickoff = kickoff
        self._secrets = secrets
        self._secret_values = secret_values

    def key(self, payload: BaseModel) -> str:
        return PROJECT_KEY

    def preview(self, payload: BaseModel) -> Preview:
        """Pure and cheap: the renderer the kickoff uses too."""
        return Preview(kind="text", title="Handoff", body=render(_payload(payload)))

    def warnings(self, payload: BaseModel) -> list[str]:
        handoff = _payload(payload)
        # A github_actions source is always auth "none" and uses the app's GitHub login.
        open_tracker = any(s.type == "mlflow" and s.auth == "none" for s in handoff.tracking)
        found = [NETWORK_WARNING] if open_tracker and handoff.grants.network else []
        # Shown before approval, where it can still be fixed; the seeder skips these contacts.
        return found + contact_problems(handoff.operations.on_call)

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return [
            CredentialSlot(index=i, label=_slot_label(spec), source_type=spec.type)
            for i, spec in enumerate(_payload(payload).tracking)
            if spec.auth == "credential"
        ]

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        """Idempotent; each part runs whatever happened to the ones before it, unless it
        depends on one that failed."""
        handoff = _payload(payload)
        parts = Parts(self._secret_values)
        stored: list[int] = []

        async def store() -> None:
            stored.append(
                await part.store_handoff(
                    self._connections, self._handoffs, project_id, proposal_id, handoff
                )
            )

        await parts.run("handoff", store)
        tracked = False
        try:
            for i, spec in enumerate(handoff.tracking):
                added = partial(
                    part.add_tracking_source,
                    self._tracking,
                    self._secrets,
                    project_id,
                    proposal_id,
                    i,
                    spec,
                )
                tracked = await parts.run(f"tracking:{i}", added) or tracked
        finally:
            drop = partial(part.drop_slots, self._secrets, project_id, proposal_id)
            await parts.run("credentials", drop, listed=False)
        grants = handoff.grants
        await parts.run(
            "network", partial(part.set_network, self._sandbox, project_id, grants.network)
        )
        level = partial(part.set_level, self._databases, project_id, grants.permission_level)
        await parts.run("level", level)
        if tracked:
            await parts.run("sync", partial(part.start_sync, self._tracking, project_id))
        if stored:
            [version] = stored
            event = partial(part.record_approved, self._events, project_id, proposal_id)
            await parts.run("event", event)
            start = partial(self._kickoff.start, project_id, version, proposal_id, handoff)
            await parts.run("kickoff", start)
        else:
            await parts.fail("kickoff", KICKOFF_NOT_STARTED)
        return parts.result()
