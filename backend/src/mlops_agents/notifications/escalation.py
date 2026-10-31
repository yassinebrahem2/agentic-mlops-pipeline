"""EscalationJob scheduled job for incident escalation (I24, I28, S4 §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mlops_agents.monitoring.incidents.contract import Incidents, IncidentView
from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.notifications.contract import Message, Notifier, OnCallPeople
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log


def _parse_dt(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class EscalationJob(ScheduledJob):
    """Scheduled job to evaluate and execute incident escalations across on-call people."""

    kind: JobKind = JobKind.ESCALATE

    def __init__(
        self,
        databases: Databases,
        incidents: Incidents,
        on_call: OnCallPeople,
        notifier: Notifier,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._on_call = on_call
        self._notifier = notifier
        self._clock = clock

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        """Calculates due escalations for critical, unacknowledged, non-snoozed incidents."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_utc = now.astimezone(UTC)

        settings = await get_monitoring_settings(self._databases, project_id)

        # Fetch active open and reopened incidents
        incidents: list[IncidentView] = []
        offset = 0
        limit = 100
        while True:
            page = await self._incidents.list(
                project_id, status=["open", "reopened"], limit=limit, offset=offset
            )
            incidents.extend(page.rows)
            if len(incidents) >= page.total or not page.rows:
                break
            offset += len(page.rows)

        if not incidents:
            return []

        people = await self._on_call.list(project_id)
        if not people:
            return []

        engine = await self._databases.project(project_id)
        dues: list[JobDue] = []

        for inc in incidents:
            if inc.severity != "critical":
                continue
            if inc.acknowledged_at is not None:
                continue
            if inc.snoozed_until is not None:
                snooze = inc.snoozed_until
                if snooze.tzinfo is None:
                    snooze = snooze.replace(tzinfo=UTC)
                if snooze.astimezone(UTC) > now_utc:
                    continue

            # Query existing notification rows for this incident
            async with engine.connect() as conn:
                stmt = (
                    select(notification_log.c.person, notification_log.c.created)
                    .where(
                        notification_log.c.incident == inc.id,
                        notification_log.c.kind.in_(["incident", "escalation"]),
                    )
                    .order_by(notification_log.c.created.asc(), notification_log.c.id.asc())
                )
                rows = (await conn.execute(stmt)).all()

            if not rows:
                continue

            sent_person_ids = {r.person for r in rows}
            next_person = next((p for p in people if p.id not in sent_person_ids), None)
            if next_person is None:
                continue

            last_created = _parse_dt(rows[-1].created)
            escalation_due = last_created + timedelta(minutes=settings.escalation_timeout_minutes)

            if escalation_due <= now_utc:
                dues.append(JobDue(kind=self.kind, due=escalation_due, subject=inc.id))

        return dues

    async def run(self, project_id: str, job: JobDue) -> None:
        """Executes the due escalation, paging the next on-call person with the updated Message."""
        incident_id = job.subject
        incident = await self._incidents.get(project_id, incident_id)
        if incident is None:
            return
        if incident.status not in ("open", "reopened"):
            return
        if incident.severity != "critical":
            return
        if incident.acknowledged_at is not None:
            return

        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_utc = now.astimezone(UTC)

        if incident.snoozed_until is not None:
            snooze = incident.snoozed_until
            if snooze.tzinfo is None:
                snooze = snooze.replace(tzinfo=UTC)
            if snooze.astimezone(UTC) > now_utc:
                return

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(
                    notification_log.c.person,
                    notification_log.c.created,
                    notification_log.c.message,
                    notification_log.c.escalation_step,
                )
                .where(
                    notification_log.c.incident == incident_id,
                    notification_log.c.kind.in_(["incident", "escalation"]),
                )
                .order_by(notification_log.c.created.asc(), notification_log.c.id.asc())
            )
            rows = (await conn.execute(stmt)).all()

        if not rows:
            return

        job_due_utc = job.due
        if job_due_utc.tzinfo is None:
            job_due_utc = job_due_utc.replace(tzinfo=UTC)
        job_due_utc = job_due_utc.astimezone(UTC)

        latest_created = _parse_dt(rows[-1].created)
        if latest_created >= job_due_utc:
            # A re-run of the same due pages no one twice (the rows are the record)
            return

        people = await self._on_call.list(project_id)
        sent_person_ids = {r.person for r in rows}
        next_person = next((p for p in people if p.id not in sent_person_ids), None)
        if next_person is None:
            # Past last person, nothing more to send
            return

        step0_raw: str | None = None
        for r in rows:
            if r.escalation_step == 0:
                step0_raw = str(r.message) if r.message is not None else None
                break
        if step0_raw is None and rows[0].message is not None:
            step0_raw = str(rows[0].message)
        if step0_raw is None:
            return

        step0_msg = Message.model_validate_json(step0_raw)
        msg = step0_msg.model_copy(
            update={
                "urgent": True,
                "cause": incident.cause,
                "diagnosis_pending": (incident.cause is None),
            }
        )

        await self._notifier.notify(
            project_id,
            next_person,
            msg,
            kind="escalation",
            incident_id=incident_id,
        )
