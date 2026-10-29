"""ProposalsService: business logic, lifecycle, concurrency and recovery (I19)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, cast

from pydantic import ValidationError

from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_prefix,
    proposal_slot_ref,
)
from mlops_agents.credentials.scrub import error_text, scrub_url_secrets
from mlops_agents.errors import DomainError, validation_error_message
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.proposals.contract import (
    ApplyResult,
    ProposalKind,
    ProposalView,
    SlotState,
)
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.slots import get_slot_states, sweep
from mlops_agents.proposals.store import ProposalRecord, ProposalStore
from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


class ProposalsService:
    """Proposals service implementing the Proposals protocol with lifecycle and concurrency."""

    def __init__(
        self,
        databases: Databases,
        project_secrets: ProjectSecrets,
        kinds: ProposalKinds,
        events: EventLog,
    ) -> None:
        self._databases = databases
        self._project_secrets = project_secrets
        self._kinds = kinds
        self._events = events
        self._store = ProposalStore(databases)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        if project_id not in self._locks:
            self._locks[project_id] = asyncio.Lock()
        return self._locks[project_id]

    async def _to_view(
        self, project_id: str, rec: ProposalRecord, kind: ProposalKind
    ) -> ProposalView:
        payload = kind.payload_model.model_validate_json(rec.payload_json)
        warnings = kind.warnings(payload)
        needed = kind.credentials_needed(payload)
        slots = await get_slot_states(self._project_secrets, project_id, rec.id, needed)
        result = (
            ApplyResult.model_validate_json(rec.result_json)
            if rec.result_json is not None
            else None
        )
        return ProposalView(
            id=rec.id,
            kind=rec.kind,
            key=rec.key,
            payload=cast(Any, payload),
            warnings=warnings,
            status=rec.status,
            agent=rec.agent,
            approval_id=rec.approval_id,
            turn_id=rec.turn_id,
            tool_call_id=rec.tool_call_id,
            result=result,
            slots=slots,
            created=datetime.fromisoformat(rec.created_at),
        )

    async def create(
        self,
        project_id: str,
        agent: str,
        kind: str,
        key: str,
        payload: dict[str, Any],
        turn_id: str,
        tool_call_id: str,
    ) -> ProposalView:
        kind_obj = self._kinds.get(kind)
        try:
            validated = kind_obj.payload_model.model_validate(payload)
        except ValidationError as exc:
            message = validation_error_message(exc)
            raise DomainError(
                "proposal_invalid_payload",
                f"Payload validation failed: {message.text}",
                status=422,
                detail={"errors": message.errors},
            ) from None

        async with self._lock_for(project_id):
            proposal_id = new_id("prp")
            rec, created = await self._store.create_draft(
                project_id,
                proposal_id=proposal_id,
                agent=agent,
                kind=kind,
                key=key,
                payload_json=validated.model_dump_json(),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            if created:
                await self._events.record(
                    project_id,
                    source=agent,
                    type="proposal.created",
                    payload={"proposal_id": rec.id, "kind": kind},
                    subject_id=rec.id,
                )
            return await self._to_view(project_id, rec, kind_obj)

    async def attach(self, project_id: str, proposal_id: str, approval_id: str) -> str | None:
        async with self._lock_for(project_id):
            _draft, other = await self._store.attach_and_supersede(
                project_id, proposal_id, approval_id
            )
            if other is not None:
                kind_obj = self._kinds.get(other.kind)
                other_payload = kind_obj.payload_model.model_validate_json(other.payload_json)
                needed = kind_obj.credentials_needed(other_payload)
                await sweep(self._project_secrets, project_id, other.id, needed)
                return other.approval_id
            return None

    async def decide(self, project_id: str, proposal_id: str, approved: bool) -> ApplyResult | None:
        async with self._lock_for(project_id):
            rec = await self._store.get(project_id, proposal_id)
            if rec is None or rec.status != "pending":
                return None

            kind_obj = self._kinds.get(rec.kind)
            payload = kind_obj.payload_model.model_validate_json(rec.payload_json)
            needed = kind_obj.credentials_needed(payload)

            if not approved:
                rowcount = await self._store.update_status(
                    project_id, proposal_id, status="rejected"
                )
                if rowcount == 0:
                    return None
                await sweep(self._project_secrets, project_id, proposal_id, needed)
                await self._events.record(
                    project_id,
                    source="system",
                    type="proposal.decided",
                    payload={"proposal_id": proposal_id, "kind": rec.kind, "status": "rejected"},
                    subject_id=proposal_id,
                )
                return None
            else:
                rowcount = await self._store.update_status(
                    project_id, proposal_id, status="approved"
                )
                if rowcount == 0:
                    return None

                result: ApplyResult = ApplyResult(applied=[], failed={})
                try:
                    result = await kind_obj.apply(project_id, proposal_id, payload)
                except Exception as exc:
                    logger.error(
                        "Failed to apply proposal %s: %s",
                        proposal_id,
                        error_text(exc),
                    )
                    result = ApplyResult(applied=[], failed={"apply": error_text(exc)})
                finally:
                    await sweep(
                        self._project_secrets, project_id, proposal_id, needed, result=result
                    )

                scrubbed_failed = {k: scrub_url_secrets(v) for k, v in result.failed.items()}
                result = ApplyResult(applied=result.applied, failed=scrubbed_failed)

                await self._store.update_result(
                    project_id, proposal_id, result_json=result.model_dump_json()
                )
                await self._events.record(
                    project_id,
                    source="system",
                    type="proposal.decided",
                    payload={"proposal_id": proposal_id, "kind": rec.kind, "status": "approved"},
                    subject_id=proposal_id,
                )
                return result

    async def get_by_approval(self, project_id: str, approval_id: str) -> ProposalView | None:
        rec = await self._store.get_by_approval(project_id, approval_id)
        if rec is None:
            return None
        kind_obj = self._kinds.get(rec.kind)
        return await self._to_view(project_id, rec, kind_obj)

    async def pending(self, project_id: str, kind: str, key: str) -> ProposalView | None:
        rec = await self._store.pending(project_id, kind, key)
        if rec is None:
            return None
        kind_obj = self._kinds.get(rec.kind)
        return await self._to_view(project_id, rec, kind_obj)

    async def latest_for_turn(
        self, project_id: str, turn_id: str, kind: str
    ) -> ProposalView | None:
        rec = await self._store.latest_for_turn(project_id, turn_id, kind)
        if rec is None:
            return None
        kind_obj = self._kinds.get(rec.kind)
        return await self._to_view(project_id, rec, kind_obj)

    async def for_call(
        self, project_id: str, turn_id: str, tool_call_id: str
    ) -> ProposalView | None:
        rec = await self._store.for_call(project_id, turn_id, tool_call_id)
        if rec is None:
            return None
        kind_obj = self._kinds.get(rec.kind)
        return await self._to_view(project_id, rec, kind_obj)

    async def put_credential(
        self, project_id: str, proposal_id: str, slot: int, value: str
    ) -> SlotState:
        async with self._lock_for(project_id):
            rec = await self._store.get(project_id, proposal_id)
            if rec is None:
                raise DomainError(
                    "proposal_not_found",
                    f"Proposal '{proposal_id}' does not exist.",
                    status=404,
                )
            kind_obj = self._kinds.get(rec.kind)
            payload = kind_obj.payload_model.model_validate_json(rec.payload_json)
            needed = kind_obj.credentials_needed(payload)
            matching_slot = next((s for s in needed if s.index == slot), None)
            if matching_slot is None:
                raise DomainError(
                    "slot_not_found",
                    f"Slot {slot} is not needed by proposal '{proposal_id}'.",
                    status=404,
                )
            if rec.status != "pending":
                raise DomainError(
                    "proposal_not_pending",
                    f"Proposal '{proposal_id}' is not pending (status: {rec.status}).",
                    status=409,
                )
            ref = proposal_slot_ref(project_id, proposal_id, slot)
            await self._project_secrets.put(ref, value)
            return SlotState(index=matching_slot.index, label=matching_slot.label, set=True)

    async def recover(self, project_id: str) -> None:
        """Startup recovery for proposals in project_id.

        1. Approved proposals with no result are applied again and swept.
        2. Pending proposals with no approval_id are marked rejected and swept.
        3. Secrets of all non-pending proposals are removed.
        """
        async with self._lock_for(project_id):
            # 1. Approved without result
            approved_unapplied = await self._store.list_recover_approved_without_result(project_id)
            for rec in approved_unapplied:
                try:
                    kind_obj = self._kinds.get(rec.kind)
                    payload = kind_obj.payload_model.model_validate_json(rec.payload_json)
                    needed = kind_obj.credentials_needed(payload)
                except Exception as exc:
                    logger.error(
                        "Unknown or invalid proposal kind %r for proposal %s during recover: %s",
                        rec.kind,
                        rec.id,
                        error_text(exc),
                    )
                    result = ApplyResult(
                        applied=[],
                        failed={"kind": scrub_url_secrets(error_text(exc))},
                    )
                    await self._store.update_result(
                        project_id, rec.id, result_json=result.model_dump_json()
                    )
                    continue

                result: ApplyResult = ApplyResult(applied=[], failed={})
                try:
                    result = await kind_obj.apply(project_id, rec.id, payload)
                except Exception as exc:
                    logger.error(
                        "Failed to apply proposal %s during recover: %s",
                        rec.id,
                        error_text(exc),
                    )
                    result = ApplyResult(applied=[], failed={"apply": error_text(exc)})
                finally:
                    await sweep(
                        self._project_secrets,
                        project_id,
                        rec.id,
                        needed,
                        result=result,
                    )
                scrubbed_failed = {k: scrub_url_secrets(v) for k, v in result.failed.items()}
                result = ApplyResult(applied=result.applied, failed=scrubbed_failed)
                await self._store.update_result(
                    project_id, rec.id, result_json=result.model_dump_json()
                )

            # 2. Pending without approval
            pending_no_apr = await self._store.list_recover_pending_without_approval(project_id)
            for rec in pending_no_apr:
                await self._store.update_status(project_id, rec.id, status="rejected")
                await self._events.record(
                    project_id,
                    source="system",
                    type="proposal.decided",
                    payload={"proposal_id": rec.id, "kind": rec.kind, "status": "rejected"},
                    subject_id=rec.id,
                )
                needed = []
                try:
                    kind_obj = self._kinds.get(rec.kind)
                    payload = kind_obj.payload_model.model_validate_json(rec.payload_json)
                    needed = kind_obj.credentials_needed(payload)
                except Exception as exc:
                    logger.error(
                        "Failed to load kind %r for rejected proposal %s during recover: %s",
                        rec.kind,
                        rec.id,
                        error_text(exc),
                    )
                await sweep(self._project_secrets, project_id, rec.id, needed)

            # 3. Non-pending proposals: clean up any remaining secret refs
            non_pending = await self._store.list_non_pending(project_id)
            for rec in non_pending:
                await self._project_secrets.delete_prefix(proposal_prefix(project_id, rec.id))
