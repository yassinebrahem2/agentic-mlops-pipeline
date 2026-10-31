"""team_gate mode observer: records team gate promotion decisions and notifies on-call (Task 5, #499)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso, utc_now
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.notifications.contract import Message, Notifier, OnCallPeople
from mlops_agents.promotion.contract import EvaluationReader, Reports
from mlops_agents.promotion.promotions import SqlitePromotions
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.tracking.contract import ModelVersionRow, RunRow, Tracking, TrackingQuery
from mlops_agents.training.constants import REQUEST_TAG

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

CURSOR_KEY = "promotion_gate_cursor"


class TeamGateObserver(SyncObserver):
    """SyncObserver that checks team_gate decisions for candidate model versions.

    The app never deploys here: no hook call exists in this observer or module.
    """

    def __init__(
        self,
        databases: Databases,
        tracking: Tracking,
        handoffs: Handoffs,
        reader: EvaluationReader,
        reports: Reports,
        promotions: SqlitePromotions,
        notifier: Notifier,
        on_call: OnCallPeople,
        clock: Any | None = None,
    ) -> None:
        self._databases = databases
        self._tracking = tracking
        self._handoffs = handoffs
        self._reader = reader
        self._reports = reports
        self._promotions = promotions
        self._notifier = notifier
        self._on_call = on_call
        self._clock = clock

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
            now_dt = (
                self._clock.now()
                if (self._clock is not None and hasattr(self._clock, "now"))
                else utc_now()
            )
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=UTC)
        else:
            now_dt = now.astimezone(UTC)

        cursor = await self._get_cursor(project_id)
        if cursor is None:
            await self._set_cursor(project_id, now_dt)
            return

        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            return

        gate_models = [m for m in handoff.payload.models if m.promotion == "team_gate"]
        if not gate_models:
            return

        all_seen_created: list[datetime] = []
        waiting = False

        for model_spec in gate_models:
            page = await self._tracking.query(
                project_id,
                TrackingQuery(what="versions", model=model_spec.name, since=cursor, limit=500),
            )
            version_rows = [
                r
                for r in page.rows
                if isinstance(r, ModelVersionRow)
                and (
                    r.created.astimezone(UTC) if r.created.tzinfo else r.created.replace(tzinfo=UTC)
                )
                > cursor
            ]
            version_rows.sort(
                key=lambda r: (
                    r.created.astimezone(UTC) if r.created.tzinfo else r.created.replace(tzinfo=UTC)
                )
            )

            for v in version_rows:
                v_created = (
                    v.created.astimezone(UTC) if v.created.tzinfo else v.created.replace(tzinfo=UTC)
                )
                all_seen_created.append(v_created)

                if v.run_key is None:
                    continue

                runs_page = await self._tracking.query(
                    project_id,
                    TrackingQuery(what="runs", run_key=v.run_key),
                )
                run_row = next(
                    (
                        row
                        for row in runs_page.rows
                        if isinstance(row, RunRow) and row.key == v.run_key
                    ),
                    None,
                )
                if run_row is None or REQUEST_TAG not in run_row.tags:
                    # Run was not requested by app (team's own run); ignore
                    continue

                eval_view = await self._reader.for_candidate(project_id, model_spec.name, v.version)
                if eval_view is None or eval_view.payload.decision is None:
                    # A record with no decision waits for the next sync
                    waiting = True
                    continue

                decision = eval_view.payload.decision
                reasons = eval_view.payload.reasons
                champion_version = eval_view.payload.champion_version

                report = await self._reports.for_candidate(project_id, model_spec.name, v.version)
                report_id = report.id if report is not None else None

                promo_view = await self._promotions.record_team_gate(
                    project_id,
                    model=model_spec.name,
                    candidate_version=v.version,
                    champion_version=champion_version,
                    decision=decision,
                    reasons=reasons,
                    report_id=report_id,
                    created=now_dt,
                )

                if promo_view is not None:
                    # Row was newly recorded: notify first on-call person
                    people = await self._on_call.list(project_id)
                    if people:
                        first_person = min(people, key=lambda p: (p.escalation_order, p.id))
                        gate_reasons = reasons or []
                        msg_lines = [
                            f"Model: {model_spec.name}",
                            f"Candidate version: {v.version}",
                            f"Decision: {decision}",
                        ]
                        if gate_reasons:
                            msg_lines.extend(gate_reasons)
                        message = Message(
                            title=f"Team gate {decision}: {model_spec.name} {v.version}",
                            severity="low",
                            lines=msg_lines,
                            urgent=False,
                        )
                        await self._notifier.notify(
                            project_id,
                            first_person,
                            message,
                            kind="promotion",
                            incident_id=None,
                        )

        if all_seen_created and not waiting:
            new_cursor = max(all_seen_created)
            await self._set_cursor(project_id, new_cursor)
