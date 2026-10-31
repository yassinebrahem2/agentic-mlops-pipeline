"""Morning reports building and scheduled job (I24, I28, S4 §8, §13, §14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select

from mlops_agents.agents.approvals.contract import Approvals
from mlops_agents.events.contract import EventLog, EventRecord
from mlops_agents.monitoring.expectations.contract import ExpectationKey, Expectations
from mlops_agents.monitoring.hooks.contract import Hooks
from mlops_agents.monitoring.incidents.contract import Incidents, IncidentView, Severity
from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.scheduler.times import next_occurrences
from mlops_agents.notifications.contract import (
    Message,
    MorningReportBody,
    MorningReports,
    Notifier,
    OnCallPeople,
    ReportAction,
)
from mlops_agents.notifications.links import inbox_link
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.proposals.payloads import Cadence
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log

SEVERITY_ORDER: Final[dict[Severity, int]] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def _parse_dt(val: str | datetime) -> datetime:
    dt = val if isinstance(val, datetime) else datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_empty_report(body: MorningReportBody) -> bool:
    """An empty report (nothing in any part) is not sent."""
    return not (
        body.incidents
        or body.actions
        or body.waiting
        or body.suspended
        or body.resolved_offline
        or body.wakes_skipped
    )


def report_message(
    body: MorningReportBody,
    incidents_map: dict[str, IncidentView] | None = None,
    *,
    link: str | None = None,
    public_url: str | None = None,
    project_id: str | None = None,
) -> Message:
    """One renderer of a report into a Message."""
    severities: list[Severity] = []
    lines: list[str] = []

    inc_map = incidents_map or {}
    for inc_id in body.incidents:
        inc = inc_map.get(inc_id)
        if inc is not None:
            severities.append(inc.severity)
            title = getattr(inc, "title", None) or inc.id
            offline_note = " (resolved offline)" if inc_id in body.resolved_offline else ""
            lines.append(f"[{inc.severity.upper()}] {title}: {inc.status}{offline_note}")
        else:
            offline_note = " (resolved offline)" if inc_id in body.resolved_offline else ""
            lines.append(f"Incident {inc_id}{offline_note}")

    if body.suspended:
        for key in body.suspended:
            ref_str = f"{key.kind}:{key.ref}"
            if key.model:
                ref_str += f" ({key.model})"
            lines.append(f"Suspended expectation: {ref_str}")

    if body.wakes_skipped:
        for wake in body.wakes_skipped:
            lines.append(wake)

    highest_sev: Severity = (
        max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0)) if severities else "low"
    )

    done: list[str] = []
    for a in body.actions:
        item = f"{a.action} ({a.basis})"
        if a.why:
            item += f": {a.why}"
        done.append(item)

    waiting: list[str] = list(body.waiting)

    computed_link = link
    if computed_link is None and public_url and project_id:
        computed_link = inbox_link(public_url, project_id)

    return Message(
        title=f"Morning report for {body.person}",
        severity=highest_sev,
        done=done,
        waiting=waiting,
        lines=lines,
        link=computed_link,
        urgent=False,
    )


class SqliteMorningReports(MorningReports):
    """Builds the morning report body from incidents, hooks, approvals, expectations, and event log."""

    def __init__(
        self,
        databases: Databases,
        incidents: Incidents,
        hooks: Hooks,
        approvals: Approvals,
        expectations: Expectations,
        events: EventLog,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._hooks = hooks
        self._approvals = approvals
        self._expectations = expectations
        self._events = events

    async def build(self, project_id: str, person_id: str, since: datetime) -> MorningReportBody:
        since_utc = _parse_dt(since)

        # 1. Incidents detected since since_utc, excluding merged
        all_incidents: list[IncidentView] = []
        offset = 0
        limit = 100
        while True:
            page = await self._incidents.list(project_id, limit=limit, offset=offset)
            all_incidents.extend(page.rows)
            if len(all_incidents) >= page.total or not page.rows:
                break
            offset += len(page.rows)

        incidents_list: list[str] = []
        resolved_offline_list: list[str] = []
        for inc in all_incidents:
            if inc.status == "merged":
                continue
            det_utc = _parse_dt(inc.detected_at)
            if det_utc >= since_utc:
                incidents_list.append(inc.id)
                if inc.resolved_offline:
                    resolved_offline_list.append(inc.id)

        # 2. Actions: hook runs (non-hold)
        actions: list[ReportAction] = []
        hook_runs = await self._hooks.runs(project_id, since=since_utc)
        for r in hook_runs:
            if r.kind == "hold":
                continue
            basis_val = "bypass" if r.basis == "none" else r.basis
            actions.append(
                ReportAction(
                    incident_id=r.incident_id,
                    action=r.kind,
                    why=r.reason,
                    basis=basis_val,
                    basis_ref=r.basis_ref,
                )
            )

        # 3. Events: incident.hold_recorded and incident.wake_skipped since since_utc
        before_seq: int | None = None
        events_found: list[EventRecord] = []
        stop = False
        while not stop:
            batch = await self._events.page(
                project_id,
                types=["incident.hold_recorded", "incident.wake_skipped"],
                before=before_seq,
                limit=100,
            )
            if not batch:
                break
            for evt in batch:
                evt_dt = _parse_dt(evt.ts)
                if evt_dt < since_utc:
                    stop = True
                    break
                events_found.append(evt)
            before_seq = batch[-1].seq

        # Process events in chronological order (oldest to newest)
        wakes_skipped: list[str] = []
        for evt in reversed(events_found):
            if evt.type == "incident.hold_recorded":
                reason = str(evt.payload.get("reason", ""))
                actions.append(
                    ReportAction(
                        incident_id=evt.subject_id,
                        action="hold",
                        why=reason,
                        basis="tier1",
                        basis_ref=None,
                    )
                )
            elif evt.type == "incident.wake_skipped":
                wakes_skipped.append(f"Incident {evt.subject_id}: wake skipped (cap reached)")

        # 4. Waiting approvals: one line per pending approval
        pending_apps = await self._approvals.pending(project_id)
        waiting: list[str] = [
            f"{app.agent}: {app.proposal_kind or app.tool or 'approval'}" for app in pending_apps
        ]

        # 5. Suspended expectations: keys whose state is "suspended"
        expectation_states = await self._expectations.states(project_id)
        suspended: list[ExpectationKey] = [
            st.key for st in expectation_states if st.status == "suspended"
        ]

        return MorningReportBody(
            person=person_id,
            since=since_utc,
            incidents=incidents_list,
            actions=actions,
            waiting=waiting,
            suspended=suspended,
            resolved_offline=resolved_offline_list,
            wakes_skipped=wakes_skipped,
        )


class MorningReportJob(ScheduledJob):
    """Scheduled job evaluating and executing the morning report per on-call person (JobKind.MORNING_REPORT)."""

    kind: JobKind = JobKind.MORNING_REPORT

    def __init__(
        self,
        databases: Databases,
        on_call: OnCallPeople,
        notifier: Notifier,
        reports: MorningReports,
        clock: Clock,
        incidents: Incidents,
        settings: IntegrationSettings | None = None,
    ) -> None:
        self._databases = databases
        self._on_call = on_call
        self._notifier = notifier
        self._reports = reports
        self._clock = clock
        self._incidents = incidents
        self._settings = settings

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        """Calculates due morning reports per on-call person."""
        after_utc = _parse_dt(after)
        now_utc = _parse_dt(now)

        if after_utc >= now_utc:
            return []

        people = await self._on_call.list(project_id)
        if not people:
            return []

        engine = await self._databases.project(project_id)
        dues: list[JobDue] = []

        for person in people:
            if not person.hours or not person.hours.days:
                continue

            if not person.hours.start or ":" not in person.hours.start:
                continue
            parts = person.hours.start.split(":")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            start_h, start_m = int(parts[0]), int(parts[1])

            days_str = ",".join(person.hours.days)
            cron_expr = f"{start_m} {start_h} * * {days_str}"
            cadence = Cadence(cron=cron_expr, every_minutes=None)

            # S4 #540: Determine cursor from notification_log for this person
            async with engine.connect() as conn:
                stmt = (
                    select(notification_log.c.created)
                    .where(
                        notification_log.c.person == person.id,
                        notification_log.c.kind == "report",
                    )
                    .order_by(notification_log.c.created.desc(), notification_log.c.id.desc())
                    .limit(1)
                )
                row = (await conn.execute(stmt)).first()

            person_after = _parse_dt(row.created) if row is not None else after_utc

            occurrences = next_occurrences(cadence, person.timezone, person_after, now_utc)
            if not occurrences:
                continue

            # After downtime, only the latest missed occurrence per person is emitted
            latest_occ = occurrences[-1]
            dues.append(JobDue(kind=self.kind, due=latest_occ, subject=person.id))

        return dues

    async def run(self, project_id: str, job: JobDue) -> None:
        """Executes the morning report for a person."""
        person_id = job.subject
        people = await self._on_call.list(project_id)
        person = next((p for p in people if p.id == person_id), None)
        if person is None:
            return

        now = self._clock.now()
        now_utc = _parse_dt(now)
        job_due_utc = _parse_dt(job.due)

        engine = await self._databases.project(project_id)
        # Re-run idempotency: if a report row for this person was already created >= job.due, do nothing
        async with engine.connect() as conn:
            stmt = (
                select(notification_log.c.created)
                .where(
                    notification_log.c.person == person.id,
                    notification_log.c.kind == "report",
                )
                .order_by(notification_log.c.created.desc(), notification_log.c.id.desc())
                .limit(1)
            )
            row = (await conn.execute(stmt)).first()

        if row is not None:
            latest_created = _parse_dt(row.created)
            if latest_created >= job_due_utc:
                return

        # Find the last report row that was successfully sent (status == "sent") to determine since
        async with engine.connect() as conn:
            stmt_sent = (
                select(notification_log.c.created)
                .where(
                    notification_log.c.person == person.id,
                    notification_log.c.kind == "report",
                    notification_log.c.status == "sent",
                )
                .order_by(notification_log.c.created.desc(), notification_log.c.id.desc())
                .limit(1)
            )
            sent_row = (await conn.execute(stmt_sent)).first()

        if sent_row is not None:
            since = _parse_dt(sent_row.created)
        else:
            since = now_utc - timedelta(hours=24)

        body = await self._reports.build(project_id, person.id, since)
        if is_empty_report(body):
            return

        incidents_map: dict[str, IncidentView] = {}
        for inc_id in body.incidents:
            inc = await self._incidents.get(project_id, inc_id)
            if inc is not None:
                incidents_map[inc_id] = inc

        public_url = await self._settings.public_url() if self._settings is not None else None
        msg = report_message(body, incidents_map, public_url=public_url, project_id=project_id)

        await self._notifier.notify(
            project_id,
            person,
            msg,
            kind="report",
            incident_id=None,
        )
