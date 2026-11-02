"""Database store for results and result_sources tables (I22)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.ids import new_id
from mlops_agents.monitoring.results.constants import DETAIL_MAX_BYTES, QUERY_LIMIT_MAX
from mlops_agents.monitoring.results.contract import (
    ResultQuery,
    ResultRecord,
    SourceState,
    StoredResult,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import result_sources, results
from mlops_agents.tracking.contract import Page


def _parse_iso(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def cap_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Ensures detail serialized to JSON does not exceed DETAIL_MAX_BYTES."""
    encoded = json.dumps(detail).encode("utf-8")
    if len(encoded) <= DETAIL_MAX_BYTES:
        return detail
    return {
        "truncated": True,
        "error": f"detail exceeded {DETAIL_MAX_BYTES} bytes",
        "preview": encoded[:1000].decode("utf-8", errors="ignore"),
    }


def _row_to_stored_result(d: Mapping[Any, Any]) -> StoredResult:
    ran_at = _parse_iso(str(d["ran_at"]))
    seen_at = _parse_iso(str(d["seen_at"]))

    detail_val = d["detail"]
    if isinstance(detail_val, str):
        detail_dict = cast(dict[str, Any], json.loads(detail_val))
    elif isinstance(detail_val, dict):
        detail_dict = cast(dict[str, Any], detail_val)
    else:
        detail_dict = {}

    return StoredResult(
        id=str(d["id"]),
        check_id=str(d["check_id"]),
        origin=d["origin"],
        ran_at=ran_at,
        fired=bool(d["fired"]),
        value=float(d["value"]) if d["value"] is not None else None,
        threshold=float(d["threshold"]) if d["threshold"] is not None else None,
        category=d["category"],
        subject=str(d["subject"]),
        model=str(d["model"]) if d["model"] is not None else None,
        detail=detail_dict,
        source_ref=str(d["source_ref"]),
        version=str(d["version"]) if d["version"] is not None else None,
        not_enough_data=bool(d["not_enough_data"]),
        source_id=str(d["source_id"]),
        source_key=str(d["source_key"]),
        seen_at=seen_at,
    )


class ResultsStore:
    """Operations on the project's results and result_sources tables."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def insert_items(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
    ) -> list[StoredResult]:
        """Inserts items idempotently. Returns only the rows actually inserted."""
        if not items:
            return []

        rows = [
            {
                "id": new_id("res"),
                "check_id": record.check_id,
                "origin": record.origin,
                "ran_at": to_iso(record.ran_at),
                "fired": 1 if record.fired else 0,
                "value": record.value,
                "threshold": record.threshold,
                "category": record.category,
                "subject": record.subject,
                "model": record.model,
                "detail": json.dumps(cap_detail(record.detail)),
                "source_ref": record.source_ref,
                "version": record.version,
                "not_enough_data": 1 if record.not_enough_data else 0,
                "source_id": source_id,
                "source_key": source_key,
                "seen_at": to_iso(seen_at),
                "delivered_at": None,
            }
            for source_key, record in items
        ]

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(results)
                .on_conflict_do_nothing(index_elements=["source_id", "source_key"])
                .returning(
                    results.c.id,
                    results.c.check_id,
                    results.c.origin,
                    results.c.ran_at,
                    results.c.fired,
                    results.c.value,
                    results.c.threshold,
                    results.c.category,
                    results.c.subject,
                    results.c.model,
                    results.c.detail,
                    results.c.source_ref,
                    results.c.version,
                    results.c.not_enough_data,
                    results.c.source_id,
                    results.c.source_key,
                    results.c.seen_at,
                )
            )
            result = (await conn.execute(stmt, rows)).mappings().all()

        return [_row_to_stored_result(r) for r in result]

    async def save_items_and_state(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
        state: SourceState,
    ) -> list[StoredResult]:
        """Atomically inserts new items and updates the source cursor/state in one transaction."""
        engine = await self._databases.project(project_id)
        inserted: list[StoredResult] = []

        async with engine.begin() as conn:
            if items:
                rows = [
                    {
                        "id": new_id("res"),
                        "check_id": record.check_id,
                        "origin": record.origin,
                        "ran_at": to_iso(record.ran_at),
                        "fired": 1 if record.fired else 0,
                        "value": record.value,
                        "threshold": record.threshold,
                        "category": record.category,
                        "subject": record.subject,
                        "model": record.model,
                        "detail": json.dumps(cap_detail(record.detail)),
                        "source_ref": record.source_ref,
                        "version": record.version,
                        "not_enough_data": 1 if record.not_enough_data else 0,
                        "source_id": source_id,
                        "source_key": source_key,
                        "seen_at": to_iso(seen_at),
                        "delivered_at": None,
                    }
                    for source_key, record in items
                ]
                stmt = (
                    sqlite_insert(results)
                    .on_conflict_do_nothing(index_elements=["source_id", "source_key"])
                    .returning(
                        results.c.id,
                        results.c.check_id,
                        results.c.origin,
                        results.c.ran_at,
                        results.c.fired,
                        results.c.value,
                        results.c.threshold,
                        results.c.category,
                        results.c.subject,
                        results.c.model,
                        results.c.detail,
                        results.c.source_ref,
                        results.c.version,
                        results.c.not_enough_data,
                        results.c.source_id,
                        results.c.source_key,
                        results.c.seen_at,
                    )
                )
                res = (await conn.execute(stmt, rows)).mappings().all()
                inserted = [_row_to_stored_result(r) for r in res]

            # Upsert source state
            source_stmt = (
                sqlite_insert(result_sources)
                .values(
                    source_id=state.source_id,
                    kind=state.kind,
                    cursor=json.dumps(state.cursor),
                    last_read=to_iso(state.last_read) if state.last_read else None,
                    last_error=state.last_error,
                    consecutive_failures=state.consecutive_failures,
                )
                .on_conflict_do_update(
                    index_elements=["source_id"],
                    set_={
                        "cursor": json.dumps(state.cursor),
                        "last_read": to_iso(state.last_read) if state.last_read else None,
                        "last_error": state.last_error,
                        "consecutive_failures": state.consecutive_failures,
                    },
                )
            )
            await conn.execute(source_stmt)

        return inserted

    async def save_source_state(self, project_id: str, state: SourceState) -> None:
        """Upsert source cursor and status."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(result_sources)
                .values(
                    source_id=state.source_id,
                    kind=state.kind,
                    cursor=json.dumps(state.cursor),
                    last_read=to_iso(state.last_read) if state.last_read else None,
                    last_error=state.last_error,
                    consecutive_failures=state.consecutive_failures,
                )
                .on_conflict_do_update(
                    index_elements=["source_id"],
                    set_={
                        "cursor": json.dumps(state.cursor),
                        "last_read": to_iso(state.last_read) if state.last_read else None,
                        "last_error": state.last_error,
                        "consecutive_failures": state.consecutive_failures,
                    },
                )
            )
            await conn.execute(stmt)

    async def get_source_state(self, project_id: str, source_id: str) -> SourceState | None:
        """Fetch the source's recorded state, or None if not yet stored."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(result_sources).where(result_sources.c.source_id == source_id)
                    )
                )
                .mappings()
                .first()
            )

        if row is None:
            return None

        return SourceState(
            source_id=str(row["source_id"]),
            kind=row["kind"],
            cursor=cast(dict[str, Any], json.loads(str(row["cursor"]))),
            last_read=_parse_iso(str(row["last_read"])) if row["last_read"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
            consecutive_failures=int(row["consecutive_failures"]),
        )

    async def fetch_undelivered(
        self, project_id: str, limit: int = QUERY_LIMIT_MAX
    ) -> list[StoredResult]:
        """Fetch undelivered results (delivered_at IS NULL) sorted by ran_at ASC, id ASC."""
        engine = await self._databases.project(project_id)
        stmt = (
            select(results)
            .where(results.c.delivered_at.is_(None))
            .order_by(results.c.ran_at.asc(), results.c.id.asc())
            .limit(limit)
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()

        return [_row_to_stored_result(r) for r in rows]

    async def increment_delivery_attempts(
        self, project_id: str, ids: Sequence[str]
    ) -> dict[str, int]:
        """Increment delivery_attempts by 1 for the specified results, returning id -> new count."""
        if not ids:
            return {}
        engine = await self._databases.project(project_id)
        stmt = (
            update(results)
            .where(results.c.id.in_(list(ids)))
            .values(delivery_attempts=results.c.delivery_attempts + 1)
            .returning(results.c.id, results.c.delivery_attempts)
        )
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt)).all()
        return {str(r[0]): int(r[1]) for r in rows}

    async def get_delivery_attempts(self, project_id: str, ids: Sequence[str]) -> dict[str, int]:
        """Fetch delivery_attempts for the specified results."""
        if not ids:
            return {}
        engine = await self._databases.project(project_id)
        stmt = select(results.c.id, results.c.delivery_attempts).where(results.c.id.in_(list(ids)))
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {str(r[0]): int(r[1]) for r in rows}

    async def mark_delivered(
        self, project_id: str, ids: Sequence[str], delivered_at: datetime
    ) -> None:
        """Set delivered_at on the specified results."""
        if not ids:
            return
        engine = await self._databases.project(project_id)
        stmt = (
            update(results)
            .where(results.c.id.in_(list(ids)))
            .values(delivered_at=to_iso(delivered_at))
        )
        async with engine.begin() as conn:
            await conn.execute(stmt)

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        """Query stored results according to ResultQuery filters and pagination."""
        if q.limit > QUERY_LIMIT_MAX:
            raise ValueError(f"limit cannot exceed {QUERY_LIMIT_MAX}")

        filters: list[Any] = []
        if q.ids is not None:
            filters.append(results.c.id.in_(q.ids))
        if q.check_id is not None:
            filters.append(results.c.check_id == q.check_id)
        if q.model is not None:
            filters.append(results.c.model == q.model)
        if q.origin is not None:
            filters.append(results.c.origin == q.origin)
        if q.subject is not None:
            filters.append(results.c.subject == q.subject)
        if q.fired is not None:
            filters.append(results.c.fired == (1 if q.fired else 0))
        if q.since is not None:
            filters.append(results.c.ran_at >= to_iso(q.since))
        if q.until is not None:
            filters.append(results.c.ran_at <= to_iso(q.until))

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            if q.latest:
                rn = (
                    func.row_number()
                    .over(
                        partition_by=[results.c.check_id, results.c.model],
                        order_by=[results.c.ran_at.desc(), results.c.id.desc()],
                    )
                    .label("rn")
                )
                sub = select(results, rn).where(*filters).subquery()
                count_stmt = select(func.count()).select_from(sub).where(sub.c.rn == 1)
                total = int((await conn.scalar(count_stmt)) or 0)

                paged_stmt = (
                    select(sub)
                    .where(sub.c.rn == 1)
                    .order_by(sub.c.ran_at.desc(), sub.c.id.desc())
                    .limit(q.limit)
                    .offset(q.offset)
                )
                rows = (await conn.execute(paged_stmt)).mappings().all()
            else:
                count_stmt = select(func.count()).select_from(results).where(*filters)
                total = int((await conn.scalar(count_stmt)) or 0)

                paged_stmt = (
                    select(results)
                    .where(*filters)
                    .order_by(results.c.ran_at.desc(), results.c.id.desc())
                    .limit(q.limit)
                    .offset(q.offset)
                )
                rows = (await conn.execute(paged_stmt)).mappings().all()

        records = [_row_to_stored_result(r) for r in rows]
        truncated = total > (q.offset + len(records))
        return Page[StoredResult](rows=records, total=total, truncated=truncated)

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]:
        """Fetch the newest record of each check for the specified subject and model."""
        filters = [results.c.subject == subject]
        if model is not None:
            filters.append(results.c.model == model)
        else:
            filters.append(results.c.model.is_(None))

        rn = (
            func.row_number()
            .over(
                partition_by=results.c.check_id,
                order_by=[results.c.ran_at.desc(), results.c.id.desc()],
            )
            .label("rn")
        )
        sub = select(results, rn).where(*filters).subquery()
        stmt = select(sub).where(sub.c.rn == 1).order_by(sub.c.ran_at.desc(), sub.c.id.desc())

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()

        return [_row_to_stored_result(r) for r in rows]
