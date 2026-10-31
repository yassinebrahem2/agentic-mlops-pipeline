"""Fixes store (SqliteFixes) and Retraining wake (Task 1, #486)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select, update

from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventType
from mlops_agents.ids import new_id
from mlops_agents.monitoring.incidents.contract import AgentWaker, Incidents
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import fix_requests
from mlops_agents.training.constants import (
    EVIDENCE_MAX_CHARS,
    RETRAINING_AGENT,
    WHY_MAX_CHARS,
    WHY_NOT_MAX_CHARS,
)
from mlops_agents.training.contract import (
    FixChoice,
    FixRequestView,
    FixStatus,
    GuardRefusal,
    GuardResult,
    TrainingWindow,
)
from mlops_agents.training.guards import (
    check_data_held,
    check_labels_immature,
    check_open_data_quality_incident,
)

if TYPE_CHECKING:
    from mlops_agents.proposals.contract import Handoffs
    from mlops_agents.providers.failover import Clock

_MENU_CHOICES: tuple[FixChoice, ...] = ("re_threshold", "recalibrate", "retrain")


class SqliteFixes:
    """SQLite-backed fix requests store."""

    def __init__(
        self,
        databases: Databases,
        incidents: Incidents,
        waker: AgentWaker,
        events: EventLog,
        clock: Clock,
        handoffs: Handoffs,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._waker = waker
        self._events = events
        self._clock = clock
        self._handoffs = handoffs

    async def guards(
        self,
        project_id: str,
        model: str,
        window: TrainingWindow,
        policy_id: str | None = None,
    ) -> GuardResult:
        now = self._clock.now()
        refusals: list[GuardRefusal] = []

        training_tables: set[str] = set()
        model_spec = None

        handoff = await self._handoffs.current(project_id)
        if handoff is not None:
            training_tables = {
                dr.table for dr in handoff.payload.data_roles if dr.role == "training"
            }
            model_spec = next((m for m in handoff.payload.models if m.name == model), None)

        # 1. data_held
        refusals.extend(
            await check_data_held(self._incidents, project_id, model, training_tables, window)
        )

        # 2. open_data_quality_incident
        refusals.extend(
            await check_open_data_quality_incident(
                self._incidents, project_id, training_tables, window, now
            )
        )

        # 3. labels_immature
        immature_refusal = check_labels_immature(model_spec, window, now, model_name=model)
        if immature_refusal is not None:
            refusals.append(immature_refusal)

        return GuardResult(ok=len(refusals) == 0, refusals=refusals)

    async def request(
        self, project_id: str, incident_id: str, agent: str, evidence: str
    ) -> FixRequestView:
        incident = await self._incidents.get(project_id, incident_id)
        if incident is None:
            raise DomainError("not_found", f"Incident '{incident_id}' not found.")
        if incident.model is None:
            raise DomainError("missing_model", f"Incident '{incident_id}' does not name a model.")

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            existing_row = (
                (
                    await conn.execute(
                        select(fix_requests)
                        .where(
                            fix_requests.c.incident_id == incident_id,
                            fix_requests.c.status.in_(["requested", "chosen", "running"]),
                        )
                        .order_by(fix_requests.c.created.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )

            if existing_row is not None:
                view = self._row_to_view(existing_row)
                if view.status == "requested":
                    wake_text = (
                        f"Fix request {view.id} for incident {incident_id} (model: {incident.model}):\n"
                        f"{untrusted(view.evidence, source='fix_request')}"
                    )
                    await self._waker.wake(
                        project_id=project_id,
                        agent=RETRAINING_AGENT,
                        label="fix_request",
                        subject_id=incident_id,
                        text=wake_text,
                        key=view.id,
                    )
                return view

        capped_evidence = evidence[:EVIDENCE_MAX_CHARS]
        fix_id = new_id("fix")
        now_dt = self._clock.now()
        created_iso = now_dt.isoformat()

        async with engine.begin() as conn:
            await conn.execute(
                insert(fix_requests).values(
                    id=fix_id,
                    incident_id=incident_id,
                    model=incident.model,
                    requested_by=agent,
                    evidence=capped_evidence,
                    choice=None,
                    why=None,
                    why_not="{}",
                    status="requested",
                    outcome="pending",
                    created=created_iso,
                )
            )

        await self._events.record(
            project_id,
            source=agent,
            type="fix.requested",
            payload={
                "fix_id": fix_id,
                "incident_id": incident_id,
                "model": incident.model,
            },
            subject_id=incident_id,
        )

        wake_text = (
            f"Fix request {fix_id} for incident {incident_id} (model: {incident.model}):\n"
            f"{untrusted(capped_evidence, source='fix_request')}"
        )
        await self._waker.wake(
            project_id=project_id,
            agent=RETRAINING_AGENT,
            label="fix_request",
            subject_id=incident_id,
            text=wake_text,
            key=fix_id,
        )

        return FixRequestView(
            id=fix_id,
            incident_id=incident_id,
            model=incident.model,
            requested_by=agent,
            evidence=capped_evidence,
            choice=None,
            why=None,
            why_not={},
            status="requested",
            outcome="pending",
            created=now_dt,
        )

    async def choose(
        self,
        project_id: str,
        fix_id: str,
        agent: str,
        choice: FixChoice,
        why: str,
        why_not: dict[str, str],
    ) -> FixRequestView:
        if agent != RETRAINING_AGENT:
            raise DomainError(
                "unauthorized",
                f"Only agent '{RETRAINING_AGENT}' can choose a fix (called by '{agent}').",
            )

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(fix_requests).where(fix_requests.c.id == fix_id)))
                .mappings()
                .one_or_none()
            )

        if row is None:
            raise DomainError("not_found", f"Fix '{fix_id}' not found.")

        current_status = row["status"]
        if current_status != "requested":
            raise DomainError(
                "invalid_status",
                f"Fix '{fix_id}' is not in 'requested' status (current status: '{current_status}').",
            )

        if not why or not why.strip():
            raise DomainError("invalid_argument", "The 'why' reasoning cannot be blank.")

        if choice == "decline":
            required_options = set(_MENU_CHOICES)
        else:
            required_options = set(_MENU_CHOICES) - {choice}

        for opt in sorted(required_options):
            reason = why_not.get(opt)
            if reason is None or not reason.strip():
                raise DomainError(
                    "invalid_argument",
                    f"Missing or blank reasoning for '{opt}' in why_not.",
                )

        capped_why = why[:WHY_MAX_CHARS]
        capped_why_not = {k: v[:WHY_NOT_MAX_CHARS] for k, v in why_not.items()}

        new_status: FixStatus
        event_type: EventType
        if choice == "decline":
            new_status = "declined"
            event_type = "fix.declined"
        else:
            new_status = "chosen"
            event_type = "fix.chosen"

        async with engine.begin() as conn:
            await conn.execute(
                update(fix_requests)
                .where(fix_requests.c.id == fix_id)
                .values(
                    choice=choice,
                    why=capped_why,
                    why_not=json.dumps(capped_why_not),
                    status=new_status,
                )
            )

        await self._events.record(
            project_id,
            source=agent,
            type=event_type,
            payload={
                "fix_id": fix_id,
                "choice": choice,
                "why": capped_why,
            },
            subject_id=row["incident_id"],
        )

        created_raw = row["created"]
        created_dt = (
            datetime.fromisoformat(created_raw) if isinstance(created_raw, str) else created_raw
        )
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC)

        return FixRequestView(
            id=fix_id,
            incident_id=row["incident_id"],
            model=row["model"],
            requested_by=row["requested_by"],
            evidence=row["evidence"],
            choice=choice,
            why=capped_why,
            why_not=capped_why_not,
            status=new_status,
            outcome=row["outcome"],
            created=created_dt,
        )

    async def get(self, project_id: str, fix_id: str) -> FixRequestView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(fix_requests).where(fix_requests.c.id == fix_id)))
                .mappings()
                .one_or_none()
            )

        if row is None:
            return None

        return self._row_to_view(row)

    def _row_to_view(self, row: Any) -> FixRequestView:
        why_not_raw = row["why_not"]
        if isinstance(why_not_raw, str):
            why_not: dict[str, str] = json.loads(why_not_raw) if why_not_raw else {}
        else:
            why_not = why_not_raw or {}

        created_raw = row["created"]
        if isinstance(created_raw, str):
            created_dt = datetime.fromisoformat(created_raw)
        else:
            created_dt = created_raw
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC)

        return FixRequestView(
            id=row["id"],
            incident_id=row["incident_id"],
            model=row["model"],
            requested_by=row["requested_by"],
            evidence=row["evidence"],
            choice=row["choice"],
            why=row["why"],
            why_not=why_not,
            status=row["status"],
            outcome=row["outcome"],
            created=created_dt,
        )
