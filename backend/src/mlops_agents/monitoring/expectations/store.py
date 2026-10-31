"""SQLite store for monitoring expectations (expectations table)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import expectations


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).astimezone(UTC)


class SqliteExpectationsStore:
    """Manages the expectations table in project databases."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def register(
        self,
        project_id: str,
        spec: ExpectationSpec,
        active: bool | None = None,
    ) -> None:
        """Registers an expectation. Replaces spec if key exists."""
        if active is None:
            active = spec.activates_on_pr is None

        engine = await self._databases.project(project_id)
        spec_json = spec.model_dump_json()
        model_str = spec.key.model or ""

        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(expectations.c.kind).where(
                        expectations.c.kind == spec.key.kind,
                        expectations.c.ref == spec.key.ref,
                        func.coalesce(expectations.c.model, "") == model_str,
                    )
                )
            ).first()

            if existing is not None:
                await conn.execute(
                    update(expectations)
                    .where(
                        expectations.c.kind == spec.key.kind,
                        expectations.c.ref == spec.key.ref,
                        func.coalesce(expectations.c.model, "") == model_str,
                    )
                    .values(
                        spec=spec_json,
                        active=1 if active else 0,
                    )
                )
            else:
                await conn.execute(
                    insert(expectations).values(
                        kind=spec.key.kind,
                        ref=spec.key.ref,
                        model=spec.key.model,
                        spec=spec_json,
                        active=1 if active else 0,
                        last_seen=None,
                        last_signalled_first=None,
                        last_signalled_second=None,
                    )
                )

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        """Sets active=0 for an expectation key."""
        engine = await self._databases.project(project_id)
        model_str = key.model or ""
        async with engine.begin() as conn:
            await conn.execute(
                update(expectations)
                .where(
                    expectations.c.kind == key.kind,
                    expectations.c.ref == key.ref,
                    func.coalesce(expectations.c.model, "") == model_str,
                )
                .values(active=0)
            )

    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None:
        """Updates last_seen to ran_at if newer or currently null."""
        engine = await self._databases.project(project_id)
        model_str = key.model or ""
        ran_at_iso = to_iso(ran_at)

        async with engine.begin() as conn:
            await conn.execute(
                update(expectations)
                .where(
                    expectations.c.kind == key.kind,
                    expectations.c.ref == key.ref,
                    func.coalesce(expectations.c.model, "") == model_str,
                    (expectations.c.last_seen.is_(None) | (expectations.c.last_seen < ran_at_iso)),
                )
                .values(last_seen=ran_at_iso)
            )

    async def update_signalled(
        self,
        project_id: str,
        key: ExpectationKey,
        first_t: datetime | None = None,
        second_t: datetime | None = None,
    ) -> None:
        """Updates last_signalled_first and/or last_signalled_second."""
        engine = await self._databases.project(project_id)
        model_str = key.model or ""
        values: dict[str, Any] = {}
        if first_t is not None:
            values["last_signalled_first"] = to_iso(first_t)
        if second_t is not None:
            values["last_signalled_second"] = to_iso(second_t)

        if not values:
            return

        async with engine.begin() as conn:
            await conn.execute(
                update(expectations)
                .where(
                    expectations.c.kind == key.kind,
                    expectations.c.ref == key.ref,
                    func.coalesce(expectations.c.model, "") == model_str,
                )
                .values(**values)
            )

    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]:
        """Activates inactive expectations that match activates_on_pr."""
        engine = await self._databases.project(project_id)
        activated: list[ExpectationKey] = []

        async with engine.begin() as conn:
            rows = (
                (await conn.execute(select(expectations).where(expectations.c.active == 0)))
                .mappings()
                .all()
            )

            for row in rows:
                spec = ExpectationSpec.model_validate_json(row["spec"])
                if spec.activates_on_pr == pr_number:
                    model_str = spec.key.model or ""
                    await conn.execute(
                        update(expectations)
                        .where(
                            expectations.c.kind == spec.key.kind,
                            expectations.c.ref == spec.key.ref,
                            func.coalesce(expectations.c.model, "") == model_str,
                        )
                        .values(active=1)
                    )
                    activated.append(spec.key)

        return activated

    async def get(
        self, project_id: str, key: ExpectationKey
    ) -> tuple[ExpectationSpec, bool, datetime | None, datetime | None, datetime | None] | None:
        """Gets a single expectation record by key."""
        engine = await self._databases.project(project_id)
        model_str = key.model or ""
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(expectations).where(
                            expectations.c.kind == key.kind,
                            expectations.c.ref == key.ref,
                            func.coalesce(expectations.c.model, "") == model_str,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None

            spec = ExpectationSpec.model_validate_json(row["spec"])
            active = bool(row["active"])
            last_seen = _parse_iso(row["last_seen"])
            first_sig = _parse_iso(row["last_signalled_first"])
            second_sig = _parse_iso(row["last_signalled_second"])
            return spec, active, last_seen, first_sig, second_sig

    async def list_all(
        self, project_id: str, active_only: bool = False
    ) -> list[tuple[ExpectationSpec, bool, datetime | None, datetime | None, datetime | None]]:
        """Lists all expectation records."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(expectations)
            if active_only:
                stmt = stmt.where(expectations.c.active == 1)
            rows = (await conn.execute(stmt)).mappings().all()

            results: list[
                tuple[ExpectationSpec, bool, datetime | None, datetime | None, datetime | None]
            ] = []
            for row in rows:
                spec = ExpectationSpec.model_validate_json(row["spec"])
                active = bool(row["active"])
                last_seen = _parse_iso(row["last_seen"])
                first_sig = _parse_iso(row["last_signalled_first"])
                second_sig = _parse_iso(row["last_signalled_second"])
                results.append((spec, active, last_seen, first_sig, second_sig))
            return results

    async def retire_handoff_keys_not_in(
        self, project_id: str, current_handoff_keys: set[tuple[str, str, str]]
    ) -> list[ExpectationKey]:
        """Retires active expectations with source='handoff' whose key is not in current_handoff_keys.

        Keys from RegisterCheck (source='check') and training (source='training') are NEVER retired.
        """
        engine = await self._databases.project(project_id)
        retired: list[ExpectationKey] = []

        async with engine.begin() as conn:
            rows = (
                (await conn.execute(select(expectations).where(expectations.c.active == 1)))
                .mappings()
                .all()
            )

            for row in rows:
                spec = ExpectationSpec.model_validate_json(row["spec"])
                if spec.source != "handoff":
                    continue

                k_tuple = (spec.key.kind, spec.key.ref, spec.key.model or "")
                if k_tuple not in current_handoff_keys:
                    model_str = spec.key.model or ""
                    await conn.execute(
                        update(expectations)
                        .where(
                            expectations.c.kind == spec.key.kind,
                            expectations.c.ref == spec.key.ref,
                            func.coalesce(expectations.c.model, "") == model_str,
                        )
                        .values(active=0)
                    )
                    retired.append(spec.key)

        return retired
