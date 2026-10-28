"""Proposal storage backed by project.db (I19, I20), SQL only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.proposals.contract import ProposalStatus
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import proposals


@dataclass
class ProposalRecord:
    id: str
    kind: str
    key: str
    payload_json: str
    status: ProposalStatus
    agent: str
    approval_id: str | None
    turn_id: str
    tool_call_id: str
    result_json: str | None
    created_at: str
    decided_at: str | None


def _row_to_record(row: Any) -> ProposalRecord:
    return ProposalRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        key=str(row["key"]),
        payload_json=str(row["payload"]),
        status=cast(ProposalStatus, str(row["status"])),
        agent=str(row["agent"]),
        approval_id=str(row["approval_id"]) if row["approval_id"] is not None else None,
        turn_id=str(row["turn_id"]),
        tool_call_id=str(row["tool_call_id"]),
        result_json=str(row["result"]) if row["result"] is not None else None,
        created_at=str(row["created_at"]),
        decided_at=str(row["decided_at"]) if row["decided_at"] is not None else None,
    )


class ProposalStore:
    """SQL storage for proposals in project.db."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def create_draft(
        self,
        project_id: str,
        *,
        proposal_id: str,
        agent: str,
        kind: str,
        key: str,
        payload_json: str,
        turn_id: str,
        tool_call_id: str,
    ) -> tuple[ProposalRecord, bool]:
        """Creates a pending draft proposal, idempotent by (turn_id, tool_call_id).

        Returns (record, created).
        """
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.turn_id == turn_id,
                            proposals.c.tool_call_id == tool_call_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return _row_to_record(existing), False

            duplicate = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.turn_id == turn_id,
                            proposals.c.kind == kind,
                            proposals.c.key == key,
                            proposals.c.status == "pending",
                        )
                    )
                )
                .mappings()
                .first()
            )
            if duplicate is not None:
                raise DomainError(
                    "proposal_duplicate_in_step",
                    f"A pending proposal of kind {kind!r} and key {key!r} already exists in turn {turn_id!r}.",
                    status=422,
                )

            created_at = now_iso()
            await conn.execute(
                sqlite_insert(proposals).values(
                    id=proposal_id,
                    kind=kind,
                    key=key,
                    payload=payload_json,
                    status="pending",
                    agent=agent,
                    approval_id=None,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    result=None,
                    created_at=created_at,
                    decided_at=None,
                )
            )
            inserted = (
                (await conn.execute(select(proposals).where(proposals.c.id == proposal_id)))
                .mappings()
                .one()
            )
            return _row_to_record(inserted), True

    async def get(self, project_id: str, proposal_id: str) -> ProposalRecord | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(proposals).where(proposals.c.id == proposal_id)))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def get_by_approval(self, project_id: str, approval_id: str) -> ProposalRecord | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(proposals).where(proposals.c.approval_id == approval_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def for_call(
        self, project_id: str, turn_id: str, tool_call_id: str
    ) -> ProposalRecord | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.turn_id == turn_id,
                            proposals.c.tool_call_id == tool_call_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def pending(self, project_id: str, kind: str, key: str) -> ProposalRecord | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(proposals)
                        .where(
                            proposals.c.kind == kind,
                            proposals.c.key == key,
                            proposals.c.status == "pending",
                        )
                        .order_by(proposals.c.created_at.desc(), proposals.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def latest_for_turn(
        self, project_id: str, turn_id: str, kind: str
    ) -> ProposalRecord | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(proposals)
                        .where(
                            proposals.c.turn_id == turn_id,
                            proposals.c.kind == kind,
                        )
                        .order_by(proposals.c.created_at.desc(), proposals.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def attach_and_supersede(
        self, project_id: str, proposal_id: str, approval_id: str
    ) -> tuple[ProposalRecord, ProposalRecord | None]:
        """Sets approval_id on draft and conditionally marks any other pending proposal
        of the same kind and key with an approval as superseded.
        """
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            draft_row = (
                (await conn.execute(select(proposals).where(proposals.c.id == proposal_id)))
                .mappings()
                .first()
            )
            if draft_row is None:
                raise DomainError(
                    "proposal_not_found",
                    f"Proposal '{proposal_id}' does not exist.",
                    status=404,
                )
            if draft_row["status"] != "pending":
                raise DomainError(
                    "proposal_not_pending",
                    f"Proposal '{proposal_id}' is not pending (status: {draft_row['status']}).",
                    status=409,
                )

            await conn.execute(
                update(proposals)
                .where(proposals.c.id == proposal_id, proposals.c.status == "pending")
                .values(approval_id=approval_id)
            )

            # Find any other pending proposal of same kind and key with an approval
            other_row = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.kind == draft_row["kind"],
                            proposals.c.key == draft_row["key"],
                            proposals.c.status == "pending",
                            proposals.c.approval_id.isnot(None),
                            proposals.c.id != proposal_id,
                        )
                    )
                )
                .mappings()
                .first()
            )

            other_record: ProposalRecord | None = None
            if other_row is not None:
                decided_now = now_iso()
                await conn.execute(
                    update(proposals)
                    .where(
                        proposals.c.id == other_row["id"],
                        proposals.c.status == "pending",
                    )
                    .values(status="superseded", decided_at=decided_now)
                )
                other_record = _row_to_record(other_row)

            updated_draft = (
                (await conn.execute(select(proposals).where(proposals.c.id == proposal_id)))
                .mappings()
                .one()
            )
            return _row_to_record(updated_draft), other_record

    async def update_status(
        self,
        project_id: str,
        proposal_id: str,
        *,
        status: ProposalStatus,
        expected_status: str = "pending",
        decided_at: str | None = None,
    ) -> int:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            res = await conn.execute(
                update(proposals)
                .where(
                    proposals.c.id == proposal_id,
                    proposals.c.status == expected_status,
                )
                .values(status=status, decided_at=decided_at or now_iso())
            )
            return res.rowcount

    async def update_result(self, project_id: str, proposal_id: str, *, result_json: str) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(proposals).where(proposals.c.id == proposal_id).values(result=result_json)
            )

    async def list_recover_approved_without_result(self, project_id: str) -> list[ProposalRecord]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.status == "approved",
                            proposals.c.result.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_row_to_record(r) for r in rows]

    async def list_recover_pending_without_approval(self, project_id: str) -> list[ProposalRecord]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(proposals).where(
                            proposals.c.status == "pending",
                            proposals.c.approval_id.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_row_to_record(r) for r in rows]

    async def list_non_pending(self, project_id: str) -> list[ProposalRecord]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(select(proposals).where(proposals.c.status != "pending")))
                .mappings()
                .all()
            )
        return [_row_to_record(r) for r in rows]
