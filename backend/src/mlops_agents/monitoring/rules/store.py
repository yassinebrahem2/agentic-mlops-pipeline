"""SqliteRules store: SQLite implementation of Rules (I23)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from mlops_agents.clock import to_iso
from mlops_agents.ids import new_id
from mlops_agents.monitoring.rules.contract import (
    RuleSpec,
    RuleView,
    validate_rule_spec,
)
from mlops_agents.providers.failover import Clock, SystemClock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import rules


def parse_iso(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _row_to_rule_view(row: Mapping[str, Any]) -> RuleView:
    raw_spec = row["spec"]
    spec_dict = json.loads(raw_spec) if isinstance(raw_spec, str) else raw_spec
    spec = RuleSpec.model_validate(spec_dict)
    return RuleView(
        id=str(row["id"]),
        spec=spec,
        enabled=bool(row["enabled"]),
        proposal_id=str(row["proposal_id"]),
        approved=parse_iso(str(row["approved"])),
    )


class SqliteRules:
    """SQLite-backed Rules store implementing Rules protocol."""

    def __init__(self, databases: Databases, clock: Clock | None = None) -> None:
        self._databases = databases
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def list(self, project_id: str, model: str | None = None) -> list[RuleView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            query = select(rules).order_by(rules.c.approved.asc(), rules.c.id.asc())
            rows = (await conn.execute(query)).mappings().all()
            views = [_row_to_rule_view(dict(r)) for r in rows]
            if model is not None:
                views = [v for v in views if v.spec.model == model]
            return views

    async def get(self, project_id: str, rule_id: str) -> RuleView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(rules).where(rules.c.id == rule_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return _row_to_rule_view(dict(row))

    async def store(self, project_id: str, spec: RuleSpec, proposal_id: str) -> RuleView:
        validate_rule_spec(spec)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            existing = (
                (await conn.execute(select(rules).where(rules.c.proposal_id == proposal_id)))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return _row_to_rule_view(dict(existing))

        rule_id = new_id("rul")
        now = self._clock.now()
        now_str = to_iso(now)
        spec_json = spec.model_dump_json()

        async with engine.begin() as conn:
            try:
                await conn.execute(
                    sqlite_insert(rules).values(
                        id=rule_id,
                        spec=spec_json,
                        enabled=1,
                        proposal_id=proposal_id,
                        approved=now_str,
                    )
                )
            except IntegrityError:
                existing = (
                    (await conn.execute(select(rules).where(rules.c.proposal_id == proposal_id)))
                    .mappings()
                    .one()
                )
                return _row_to_rule_view(dict(existing))

        return RuleView(
            id=rule_id,
            spec=spec,
            enabled=True,
            proposal_id=proposal_id,
            approved=parse_iso(now_str),
        )

    async def set_enabled(self, project_id: str, rule_id: str, enabled: bool) -> RuleView:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            res = await conn.execute(
                update(rules).where(rules.c.id == rule_id).values(enabled=1 if enabled else 0)
            )
            if res.rowcount == 0:
                raise KeyError(f"Rule '{rule_id}' not found.")
            row = (await conn.execute(select(rules).where(rules.c.id == rule_id))).mappings().one()
            return _row_to_rule_view(dict(row))
