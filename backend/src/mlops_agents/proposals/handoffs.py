"""SqliteHandoffs: storage for handoff versions in project.db."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import now_iso
from mlops_agents.proposals.contract import HandoffObserver, HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import handoff_versions

logger = logging.getLogger(__name__)


class SqliteHandoffs:
    """Stores and retrieves handoff versions in project.db (I20)."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases
        self._observers: list[HandoffObserver] = []

    def add_observer(self, observer: HandoffObserver) -> None:
        self._observers.append(observer)

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        """Idempotent by proposal_id: stores a handoff version and returns that version."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                sqlite_insert(handoff_versions)
                .values(
                    proposal_id=proposal_id,
                    approved_at=now_iso(),
                    payload=payload.model_dump_json(),
                )
                .on_conflict_do_nothing(index_elements=[handoff_versions.c.proposal_id])
            )
            version = (
                await conn.execute(
                    select(handoff_versions.c.version).where(
                        handoff_versions.c.proposal_id == proposal_id
                    )
                )
            ).scalar_one()

        curr = await self.current(project_id)
        if curr is not None:
            for obs in self._observers:
                try:
                    await obs.stored(project_id, curr)
                except Exception as exc:
                    logger.warning(
                        "HandoffObserver %s failed for project %s: %s", obs, project_id, exc
                    )

        return int(version)

    async def current(self, project_id: str) -> HandoffView | None:
        """Returns the newest handoff version, or None if none has been stored."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(handoff_versions)
                        .order_by(handoff_versions.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return HandoffView(
            proposal_id=str(row["proposal_id"]),
            version=int(row["version"]),
            approved=datetime.fromisoformat(row["approved_at"]),
            payload=HandoffPayload.model_validate_json(row["payload"]),
        )
