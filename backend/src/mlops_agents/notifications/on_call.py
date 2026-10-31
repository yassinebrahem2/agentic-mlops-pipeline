"""OnCallPeople and OnCallSeeder implementation (I28)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.notifications.constants import ON_CALL_SEEDED_KEY
from mlops_agents.notifications.contract import OnCallPeople, OnCallPerson
from mlops_agents.proposals.contract import HandoffObserver, Handoffs, HandoffView
from mlops_agents.proposals.payloads import Contact, OnCall, OnCallHours
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import on_call_people, project_settings

logger = logging.getLogger(__name__)


class SqliteOnCallPeople(OnCallPeople):
    """SQLite-backed on-call people registry under databases.project(project_id)."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def list(self, project_id: str) -> list[OnCallPerson]:
        """Returns people ordered by escalation_order ASC, then id ASC."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(on_call_people).order_by(
                on_call_people.c.escalation_order.asc(),
                on_call_people.c.id.asc(),
            )
            rows = (await conn.execute(stmt)).all()

        result: list[OnCallPerson] = []
        for row in rows:
            contacts_data = json.loads(row.contacts)
            hours_data = json.loads(row.hours)
            result.append(
                OnCallPerson(
                    id=row.id,
                    name=row.name,
                    contacts=[Contact.model_validate(c) for c in contacts_data],
                    hours=OnCallHours.model_validate(hours_data),
                    timezone=row.timezone,
                    escalation_order=row.escalation_order,
                )
            )
        return result

    async def replace(self, project_id: str, people: Sequence[OnCall]) -> list[OnCallPerson]:
        """Generates slug IDs with collision suffixes, and replaces rows in one transaction."""
        counts: dict[str, int] = {}
        used_ids: set[str] = set()
        stored_people: list[OnCallPerson] = []

        for person in people:
            base_slug = re.sub(r"[^a-z0-9]+", "-", person.name.lower()).strip("-")
            if not base_slug:
                base_slug = "person"

            counts[base_slug] = counts.get(base_slug, 0) + 1
            count = counts[base_slug]
            candidate_id = base_slug if count == 1 else f"{base_slug}-{count}"

            while candidate_id in used_ids:
                counts[base_slug] += 1
                candidate_id = f"{base_slug}-{counts[base_slug]}"

            used_ids.add(candidate_id)
            stored_people.append(
                OnCallPerson(
                    id=candidate_id,
                    name=person.name,
                    contacts=person.contacts,
                    hours=person.hours,
                    timezone=person.timezone,
                    escalation_order=person.escalation_order,
                )
            )

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(delete(on_call_people))
            if stored_people:
                await conn.execute(
                    on_call_people.insert(),
                    [
                        {
                            "id": p.id,
                            "name": p.name,
                            "contacts": json.dumps([c.model_dump() for c in p.contacts]),
                            "hours": json.dumps(p.hours.model_dump()),
                            "timezone": p.timezone,
                            "escalation_order": p.escalation_order,
                        }
                        for p in stored_people
                    ],
                )

        return stored_people


class OnCallSeeder(HandoffObserver):
    """Seeds and reconciles OnCallPeople from approved handoffs without wiping user edits."""

    def __init__(
        self,
        people: OnCallPeople,
        handoffs: Handoffs,
        databases: Databases,
    ) -> None:
        self._people = people
        self._handoffs = handoffs
        self._databases = databases

    async def stored(self, project_id: str, handoff: HandoffView) -> None:
        """Called when a new handoff is stored. Replaces the list and records the version."""
        try:
            await self._people.replace(project_id, handoff.payload.operations.on_call)
            await self._set_seeded_version(project_id, handoff.version)
        except Exception:
            logger.exception("Failed to seed on-call for project %s", project_id)

    async def reconcile(self, project_id: str) -> None:
        """Seeds when no version was recorded yet or handoff version is newer than recorded."""
        current = await self._handoffs.current(project_id)
        if current is None:
            return

        recorded = await self._get_seeded_version(project_id)
        if recorded is None or recorded < current.version:
            await self._people.replace(project_id, current.payload.operations.on_call)
            await self._set_seeded_version(project_id, current.version)

    async def _get_seeded_version(self, project_id: str) -> int | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            val = await conn.scalar(
                select(project_settings.c.value).where(project_settings.c.key == ON_CALL_SEEDED_KEY)
            )
        if val is None:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    async def _set_seeded_version(self, project_id: str, version: int) -> None:
        engine = await self._databases.project(project_id)
        stmt = (
            sqlite_insert(project_settings)
            .values(key=ON_CALL_SEEDED_KEY, value=str(version))
            .on_conflict_do_update(
                index_elements=[project_settings.c.key],
                set_={"value": str(version)},
            )
        )
        async with engine.begin() as conn:
            await conn.execute(stmt)
