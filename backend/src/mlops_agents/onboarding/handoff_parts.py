"""One function per part of the handoff's apply. Each raises `DomainError` with the reason the
proposal result shows; the kind runs them in order and records each outcome."""

from __future__ import annotations

from typing import Protocol, cast, get_args

from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.connections.contract import Connections
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_prefix,
    proposal_slot_ref,
    tracking_ref,
)
from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.onboarding.handoff_check import check, table_references
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import GrantableLevel, HandoffPayload, TrackingSourceSpec
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Tracking


class NetworkSetting(Protocol):
    """SandboxService's network setting; neither method is on the Sandbox protocol."""

    async def get_network(self, project_id: str) -> bool: ...

    async def set_network(self, project_id: str, enabled: bool) -> bool: ...


class KickoffStarter(Protocol):
    async def start(
        self, project_id: str, version: int, proposal_id: str, payload: HandoffPayload
    ) -> list[str]: ...


async def store_handoff(
    connections: Connections,
    handoffs: Handoffs,
    project_id: str,
    proposal_id: str,
    payload: HandoffPayload,
) -> int:
    """Checks the references to the project's connections, then stores the whole payload."""
    ids = {view.name: view.id for view in await connections.list(project_id)}
    tables: dict[str, list[str] | str] = {}
    for name in sorted({conn for _, conn, _ in table_references(payload) if conn in ids}):
        try:
            tables[name] = [t.name for t in await connections.tables(project_id, ids[name])]
        except DomainError as exc:
            tables[name] = exc.message
    problems = check(payload, ids.keys(), tables)
    if problems:
        raise DomainError("handoff_references", "; ".join(problems), status=422)
    return await handoffs.store(project_id, proposal_id, payload)


async def add_tracking_source(
    tracking: Tracking,
    secrets: ProjectSecrets,
    project_id: str,
    proposal_id: str,
    index: int,
    spec: TrackingSourceSpec,
) -> None:
    """Idempotent: a source of the same type and settings is reused. A filled slot moves to the
    source; an empty one leaves the source without a credential."""
    settings = spec.settings.model_dump(mode="json")
    existing = await tracking.sources(project_id)
    source = next((s for s in existing if s.type == spec.type and s.settings == settings), None)
    if source is None:
        source = await tracking.create_source(project_id, spec.type, settings, spec.auth)
    if spec.auth == "credential":
        slot = proposal_slot_ref(project_id, proposal_id, index)
        if await secrets.is_set(slot):
            await secrets.move(slot, tracking_ref(project_id, source.id))


async def drop_slots(secrets: ProjectSecrets, project_id: str, proposal_id: str) -> None:
    """Every slot secret still there: those of sources that failed to be added."""
    try:
        await secrets.delete_prefix(proposal_prefix(project_id, proposal_id))
    except KeyStoreError as exc:
        # Its message never contains a key.
        raise DomainError("keystore_failed", error_text(exc), status=500) from None


async def set_network(sandbox: NetworkSetting, project_id: str, enabled: bool) -> None:
    """Touches the containers only for a changed value, so an unchanged one needs no Docker."""
    if enabled == await sandbox.get_network(project_id):
        return
    try:
        await sandbox.set_network(project_id, enabled)
    except SandboxUnavailable as exc:
        # Its message is written for the user.
        raise DomainError("sandbox_unavailable", str(exc), status=503) from None


async def set_level(databases: Databases, project_id: str, level: str) -> None:
    """The payload model forbids bypass; a hand-built payload is refused here too."""
    if level not in get_args(GrantableLevel):
        raise DomainError("level_refused", f"a proposal cannot set {level}", status=422)
    await set_permission_level(databases, project_id, cast(GrantableLevel, level))


async def start_sync(tracking: Tracking, project_id: str) -> None:
    """Only starts it: `tracking.synced` and each source's last error report its end."""
    await tracking.sync(project_id)


async def record_approved(events: EventLog, project_id: str, proposal_id: str) -> None:
    """Idempotent: one `handoff.approved` per proposal."""
    earlier = await events.page(
        project_id, subject_id=proposal_id, types=["handoff.approved"], limit=1
    )
    if earlier:
        return
    await events.record(
        project_id,
        source="onboarding",
        type="handoff.approved",
        subject_id=proposal_id,
        payload={"proposal_id": proposal_id},
    )
