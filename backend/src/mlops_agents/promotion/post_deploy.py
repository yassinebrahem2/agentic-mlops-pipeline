"""Post-deploy trigger: wakes Deployment & Rollback when a production alias moves (Task 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso, utc_now
from mlops_agents.monitoring.incidents.contract import AgentWaker, diagnoser
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.promotion.contract import Promotions
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.tracking.contract import DeploymentRow, Tracking, TrackingQuery
from mlops_agents.tracking.handoff_alias import resolve_candidate_aliases_fn

if TYPE_CHECKING:
    pass

CURSOR_KEY = "promotion_deploy_cursor"


class PostDeployObserver(SyncObserver):
    """SyncObserver that wakes Deployment & Rollback when a production alias moves."""

    def __init__(
        self,
        databases: Databases,
        tracking: Tracking,
        handoffs: Handoffs,
        promotions: Promotions,
        waker: AgentWaker,
    ) -> None:
        self._databases = databases
        self._tracking = tracking
        self._handoffs = handoffs
        self._promotions = promotions
        self._waker = waker
        self._handoff_error_logged: set[str] = set()

    async def _get_cursor(self, project_id: str) -> datetime | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        val = str(row)
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    async def _set_cursor(self, project_id: str, cursor: datetime) -> None:
        engine = await self._databases.project(project_id)
        stmt = (
            sqlite_insert(project_settings)
            .values(key=CURSOR_KEY, value=to_iso(cursor))
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": to_iso(cursor)},
            )
        )
        async with engine.begin() as conn:
            await conn.execute(stmt)

    async def synced(self, project_id: str, now: datetime | None = None) -> None:
        if now is None:
            now_dt = utc_now()
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=UTC)
        else:
            now_dt = now.astimezone(UTC)

        cursor = await self._get_cursor(project_id)
        if cursor is None:
            await self._set_cursor(project_id, now_dt)
            return

        page = await self._tracking.query(
            project_id,
            TrackingQuery(what="deployments", since=cursor, limit=500),
        )

        new_rows = [
            r
            for r in page.rows
            if isinstance(r, DeploymentRow)
            and (r.seen_at.astimezone(UTC) if r.seen_at.tzinfo else r.seen_at.replace(tzinfo=UTC))
            > cursor
        ]
        if not new_rows:
            return

        new_rows.sort(
            key=lambda r: (
                r.seen_at.astimezone(UTC) if r.seen_at.tzinfo else r.seen_at.replace(tzinfo=UTC)
            )
        )

        candidate_aliases_fn = await resolve_candidate_aliases_fn(
            self._handoffs,
            project_id,
            self._handoff_error_logged,
        )

        agent = diagnoser("operational", "bad_deploy")

        for row in new_rows:
            if row.baseline:
                continue
            if row.to_version is None:
                continue

            prod_aliases = candidate_aliases_fn(row.model)
            if row.alias not in prod_aliases:
                continue

            history = await self._promotions.history(project_id, model=row.model)
            applied = [
                p
                for p in history
                if p.candidate_version == row.to_version and p.status == "applied"
            ]
            matching = applied or [p for p in history if p.candidate_version == row.to_version]
            subject_id = matching[0].id if matching else row.model

            model_text = untrusted(row.model, source="tracking")
            old_ver = (
                untrusted(row.from_version, source="tracking")
                if row.from_version is not None
                else "none"
            )
            new_ver = untrusted(row.to_version, source="tracking")
            text = (
                f"Deployment of model {model_text} to production alias {row.alias}.\n"
                f"Old version: {old_ver}\n"
                f"New version: {new_ver}\n"
                f"Compare the evaluation record with the live label-based results."
            )
            key = (
                f"{row.model}:{row.alias}:{row.from_version}:{row.to_version}:{to_iso(row.seen_at)}"
            )

            await self._waker.wake(
                project_id=project_id,
                agent=agent,
                label="post_deploy",
                subject_id=subject_id,
                text=text,
                key=key,
            )

        max_seen = max(
            (r.seen_at.astimezone(UTC) if r.seen_at.tzinfo else r.seen_at.replace(tzinfo=UTC))
            for r in new_rows
        )
        await self._set_cursor(project_id, max_seen)
