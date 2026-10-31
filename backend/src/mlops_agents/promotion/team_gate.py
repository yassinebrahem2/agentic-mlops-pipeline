"""team_gate mode observer: records team gate promotion decisions and notifies on-call (Task 5, #499)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, TypeAdapter
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from mlops_agents.clock import to_iso, utc_now
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.notifications.contract import Message, Notifier, OnCallPeople
from mlops_agents.promotion.contract import EvaluationReader, Reports
from mlops_agents.promotion.promotions import SqlitePromotions
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.tracking.contract import ModelVersionRow, RunRow, Tracking, TrackingQuery
from mlops_agents.training.constants import REQUEST_TAG

# {model name: time the model's candidates were last fully handled}
CURSOR_KEY = "promotion_gate_cursor"
# One row per team_gate model, key = prefix + the exact model name: {version: time first seen}
SEEN_KEY_PREFIX = "promotion_gate_seen:"

_TIMES: TypeAdapter[dict[str, AwareDatetime]] = TypeAdapter(dict[str, AwareDatetime])


def _seen_key(model: str) -> str:
    return SEEN_KEY_PREFIX + model


async def _read_times(conn: AsyncConnection, key: str) -> dict[str, datetime]:
    raw = (
        await conn.execute(select(project_settings.c.value).where(project_settings.c.key == key))
    ).scalar_one_or_none()
    if raw is None:
        return {}
    return {name: at.astimezone(UTC) for name, at in _TIMES.validate_json(str(raw)).items()}


async def _write_times(conn: AsyncConnection, key: str, times: dict[str, datetime]) -> None:
    value = json.dumps({name: to_iso(at) for name, at in times.items()})
    await conn.execute(
        sqlite_insert(project_settings)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


async def _set_cursor(conn: AsyncConnection, model: str, at: datetime) -> None:
    # Read-modify-write inside the caller's transaction, so each model's progress is saved on
    # its own and a failure on a later model cannot undo it.
    cursors = await _read_times(conn, CURSOR_KEY)
    cursors[model] = at
    await _write_times(conn, CURSOR_KEY, cursors)


class TeamGateObserver(SyncObserver):
    """SyncObserver that checks team_gate decisions for candidate model versions.

    Each team_gate model has its own cursor, seeded when the model enters team_gate, so a
    model newly added or switched never treats its older versions as candidates, and one
    model's waiting candidate never holds another model back.

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

        handoff = await self._handoffs.current(project_id)
        gate_models = (
            [m.name for m in handoff.payload.models if m.promotion == "team_gate"]
            if handoff is not None
            else []
        )

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            cursors = await _read_times(conn, CURSOR_KEY)

        # A model that left team_gate (or the handoff) loses its state, so it is seeded again
        # if it re-enters.
        dropped = [name for name in cursors if name not in gate_models]
        if dropped:
            async with engine.begin() as conn:
                for model in dropped:
                    await conn.execute(
                        delete(project_settings).where(project_settings.c.key == _seen_key(model))
                    )
                await _write_times(
                    conn,
                    CURSOR_KEY,
                    {name: at for name, at in cursors.items() if name in gate_models},
                )

        for model in gate_models:
            versions = await self._model_versions(project_id, model)
            cursor = cursors.get(model)
            if cursor is None:
                # The seen state and the cursor entry are saved together: a seed is never half
                # done, so it is never redone over versions that arrived since.
                async with engine.begin() as conn:
                    await _write_times(
                        conn, _seen_key(model), {v.version: now_dt for v in versions}
                    )
                    await _set_cursor(conn, model, now_dt)
                continue

            async with engine.connect() as conn:
                seen = await _read_times(conn, _seen_key(model))
            unseen = {v.version: now_dt for v in versions if v.version not in seen}
            if unseen:
                seen.update(unseen)
                async with engine.begin() as conn:
                    await _write_times(conn, _seen_key(model), seen)

            # Candidates are the versions the app first saw after the cursor, oldest first.
            candidates = sorted(
                (v for v in versions if seen[v.version] > cursor),
                key=lambda v: (seen[v.version], v.version),
            )
            waiting = False
            for version in candidates:
                if await self._handle_candidate(project_id, model, version, now_dt):
                    waiting = True
            if not waiting:
                async with engine.begin() as conn:
                    await _set_cursor(conn, model, now_dt)

    async def _model_versions(self, project_id: str, model: str) -> list[ModelVersionRow]:
        # The one query for a model's versions, used for seeding and processing alike, so the
        # seeded set and the processed set cannot diverge.
        page = await self._tracking.query(
            project_id, TrackingQuery(what="versions", model=model, limit=500)
        )
        return [row for row in page.rows if isinstance(row, ModelVersionRow)]

    async def _handle_candidate(
        self, project_id: str, model: str, version: ModelVersionRow, now_dt: datetime
    ) -> bool:
        """Decide one candidate if its gate decision is in; return True when it must wait."""
        # A version already decided in any mode is never re-decided, whatever the cursor says.
        if await self._promotions.has_record(project_id, model, version.version):
            return False

        # Every app-requested version has its run, so one without a run key is the team's.
        if version.run_key is None:
            return False

        runs_page = await self._tracking.query(
            project_id, TrackingQuery(what="runs", run_key=version.run_key)
        )
        run_row = next(
            (
                row
                for row in runs_page.rows
                if isinstance(row, RunRow) and row.key == version.run_key
            ),
            None,
        )
        if run_row is None:
            # The run row has not synced yet: wait for it.
            return True

        if REQUEST_TAG not in run_row.tags:
            # The team's own run: the team's to decide.
            return False

        eval_view = await self._reader.for_candidate(project_id, model, version.version)
        if eval_view is None or eval_view.payload.decision is None:
            # A record with no decision waits for the next sync.
            return True

        decision = eval_view.payload.decision
        reasons = eval_view.payload.reasons

        report = await self._reports.for_candidate(project_id, model, version.version)
        report_id = report.id if report is not None else None

        promo_view = await self._promotions.record_team_gate(
            project_id,
            model=model,
            candidate_version=version.version,
            champion_version=eval_view.payload.champion_version,
            decision=decision,
            reasons=reasons,
            report_id=report_id,
            created=now_dt,
        )
        if promo_view is None:
            return False

        # Row was newly recorded: notify the first on-call person.
        people = await self._on_call.list(project_id)
        if not people:
            return False
        first_person = min(people, key=lambda p: (p.escalation_order, p.id))
        model_text = untrusted(model, source="tracking")
        ver_text = untrusted(version.version, source="tracking")
        msg_lines = [
            f"Model: {model_text}",
            f"Candidate version: {ver_text}",
            f"Decision: {decision}",
        ]
        msg_lines.extend(untrusted(r, source="evaluation") for r in reasons or [])
        message = Message(
            title=f"Team gate {decision}: {model_text} {ver_text}",
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
        return False
