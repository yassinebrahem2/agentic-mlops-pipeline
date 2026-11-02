"""Reactor implementation: coordinates immediate notifications, issue opening, and waking (S4 §7)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol, cast

import httpx2
from sqlalchemy import select

from mlops_agents.integrations.github.issues import create_issue, ensure_labels
from mlops_agents.monitoring.incidents.severity import SEVERITY_RANKS
from mlops_agents.notifications.contract import Message
from mlops_agents.storage.tables import notification_log

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.monitoring.incidents.contract import (
        IncidentChange,
        IncidentObserver,
        Incidents,
        Router,
    )
    from mlops_agents.notifications.contract import Notifier, OnCallPeople
    from mlops_agents.providers.failover import Clock
    from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


class ReactorIncidents(Protocol):
    async def record_issue(
        self, project_id: str, incident_id: str, issue_number: int
    ) -> list[int]: ...


class Reactor:
    """The one registered incident observer for immediate deliveries, issues, and waking."""

    def __init__(
        self,
        incidents: Incidents,
        router: Router,
        on_call: OnCallPeople,
        notifier: Notifier,
        dispatch: IncidentObserver,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
        events: EventLog,
        clock: Clock,
        databases: Databases | None = None,
    ) -> None:
        self._incidents = incidents
        self._router = router
        self._on_call = on_call
        self._notifier = notifier
        self._dispatch = dispatch
        self._tasks = tasks
        self._github = github
        self._http = http
        self._events = events
        self._clock = clock
        self._databases = databases or getattr(self._incidents, "_databases", None)

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        if change.kind == "cause":
            await self._dispatch.changed(project_id, change)
            return

        if change.kind not in ("opened", "reopened", "severity_raised", "hold"):
            return

        inc = await self._incidents.get(project_id, change.incident_id)
        if inc is None:
            return

        if inc.resolved_offline:
            return

        now = self._clock.now()
        if inc.snoozed_until is not None and now < inc.snoozed_until:
            return

        route_plan = await self._router.plan(project_id, inc, now)

        # 1. Send immediate deliveries (when == "now")
        now_deliveries = [d for d in route_plan.deliveries if d.when == "now"]
        if now_deliveries:
            paged_max_rank: dict[str, int] = {}
            if change.kind == "severity_raised" and self._databases is not None:
                engine = await self._databases.project(project_id)
                async with engine.connect() as conn:
                    stmt = select(
                        notification_log.c.person,
                        notification_log.c.message,
                    ).where(
                        notification_log.c.incident == inc.id,
                        notification_log.c.kind.in_(["incident", "escalation"]),
                    )
                    rows = (await conn.execute(stmt)).all()
                    for r in rows:
                        person_id = r[0]
                        raw_msg = r[1]
                        sev_rank = 0
                        if raw_msg:
                            try:
                                msg_data = (
                                    json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
                                )
                                sev = msg_data.get("severity")
                                sev_rank = SEVERITY_RANKS.get(sev, 0)
                            except Exception:
                                sev_rank = 0
                        paged_max_rank[person_id] = max(paged_max_rank.get(person_id, 0), sev_rank)

            people = await self._on_call.list(project_id)
            people_by_id = {p.id: p for p in people}

            lines: list[str] = []
            if change.kind == "hold":
                try:
                    events_list = await self._events.page(project_id, subject_id=inc.id, limit=5)
                    for ev in events_list:
                        if ev.type == "incident.hold_recorded":
                            scope_data = ev.payload.get("scope", {})
                            reason = ev.payload.get("reason", "")
                            tables = scope_data.get("tables", [])
                            held_str = (
                                f"Tables: {', '.join(tables)}" if tables else "Production hold"
                            )
                            if reason:
                                held_str += f" ({reason})"
                            lines.append(f"Held: {held_str}")
                            break
                except Exception:
                    logger.debug("Failed to read hold event details for incident %s", inc.id)
                if not lines:
                    lines.append("Hold placed on production")

            current_sev_rank = SEVERITY_RANKS.get(inc.severity, 0)
            for d in now_deliveries:
                if (
                    change.kind == "severity_raised"
                    and paged_max_rank.get(d.person, 0) >= current_sev_rank
                ):
                    continue
                person = people_by_id.get(d.person)
                if person is None:
                    continue

                title = f"[{inc.severity.upper()}] Incident {inc.id}: {inc.category}"
                message = Message(
                    title=title,
                    severity=inc.severity,
                    cause=inc.cause,
                    lines=lines,
                    urgent=(inc.severity == "critical"),
                    diagnosis_pending=(inc.cause is None),
                )
                try:
                    await self._notifier.notify(
                        project_id,
                        person,
                        message,
                        kind="incident",
                        incident_id=inc.id,
                    )
                except Exception:
                    logger.exception(
                        "Notifier failed for person %s on incident %s",
                        person.id,
                        inc.id,
                    )

        # 2. Issue creation
        if route_plan.open_issue and not inc.issues:
            try:
                repo_ref = await self._tasks.repo_ref(project_id)
                if repo_ref is not None:
                    token = await self._github.token()
                    if token:
                        repo_full_name = f"{repo_ref.owner}/{repo_ref.repo}"
                        labels = [f"category:{inc.category}", f"priority:{inc.severity}"]
                        await ensure_labels(self._http, token, repo_full_name, labels)
                        body = (
                            f"Incident {inc.id}\n"
                            f"Category: {inc.category}\n"
                            f"Severity: {inc.severity}\n"
                            f"Status: {inc.status}\n"
                            f"Cause: {inc.cause or 'Pending diagnosis'}"
                        )
                        created = await create_issue(
                            self._http,
                            token,
                            repo_full_name,
                            title=f"Incident {inc.id}: {inc.category}",
                            body=body,
                            labels=labels,
                        )
                        if hasattr(self._incidents, "record_issue"):
                            updated_issues = await cast(
                                ReactorIncidents, self._incidents
                            ).record_issue(project_id, inc.id, created.number)
                            await self._events.record(
                                project_id,
                                source="system",
                                type="incident.updated",
                                payload={"changed": {"issues": updated_issues}},
                                subject_id=inc.id,
                            )
            except Exception:
                logger.exception("Failed to open GitHub issue for incident %s", inc.id)

        # 3. Diagnoser dispatch
        if change.kind != "hold":
            await self._dispatch.changed(project_id, change)
