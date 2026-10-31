"""Suspect changes observer and linking for incidents (S4 §6)."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from mlops_agents.agents.pr_sync import PrChange, PrObserver
from mlops_agents.events.contract import EventLog
from mlops_agents.monitoring.incidents.contract import ACTIVE_STATUSES, Incidents
from mlops_agents.monitoring.incidents.store import IncidentStore, parse_dt
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Tracking, TrackingQuery

logger = logging.getLogger(__name__)


class SuspectLinker(SyncObserver, PrObserver):
    """Watches sync runs and merged PRs, keeping recent changes and linking suspects to active incidents."""

    def __init__(
        self,
        databases: Databases,
        incidents: Incidents,
        tracking: Tracking,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._tracking = tracking
        self._events = events
        self._clock = clock
        self._store = IncidentStore(databases)

    async def synced(self, project_id: str, now: datetime | None = None) -> None:
        """Called after each scheduler sync cycle."""
        # 1. Store CI runs
        try:
            ci_page = await self._tracking.query(
                project_id, TrackingQuery(what="ci_runs", limit=100)
            )
            for run in ci_page.rows:
                run_ref = f"{run.source_id}:{run.key}" if run.source_id else str(run.key)
                await self._store.insert_recent_change(
                    project_id,
                    kind="ci_run",
                    ref=run_ref,
                    at=run.start,
                    detail={
                        "source_id": run.source_id,
                        "key": run.key,
                        "workflow": run.workflow,
                        "status": run.status,
                        "duration_s": run.duration_s,
                        "commit": run.commit,
                        "url": run.url,
                    },
                )
        except Exception:
            logger.exception("Failed querying ci_runs in SuspectLinker for project %s", project_id)

        # 2. Store deployments
        try:
            dep_page = await self._tracking.query(
                project_id, TrackingQuery(what="deployments", limit=100)
            )
            for dep in dep_page.rows:
                dep_ref = (
                    f"{dep.source_id}:{dep.model}:{dep.alias}:{dep.to_version}"
                    if dep.source_id
                    else f"{dep.model}:{dep.alias}:{dep.to_version}"
                )
                await self._store.insert_recent_change(
                    project_id,
                    kind="deployment",
                    ref=dep_ref,
                    at=dep.seen_at,
                    detail={
                        "source_id": dep.source_id,
                        "model": dep.model,
                        "alias": dep.alias,
                        "from_version": dep.from_version,
                        "to_version": dep.to_version,
                    },
                )
        except Exception:
            logger.exception(
                "Failed querying deployments in SuspectLinker for project %s", project_id
            )

        # 3. Link suspects to active incidents
        await self._link_suspects_for_active_incidents(project_id)

    async def changed(self, project_id: str, pr: PrChange) -> None:
        """Called on PR status change."""
        if pr.state == "merged" and pr.merged_at is not None:
            await self._store.insert_recent_change(
                project_id,
                kind="agent_pr",
                ref=str(pr.number),
                at=pr.merged_at,
                detail={
                    "number": pr.number,
                    "state": pr.state,
                    "head_branch": pr.head_branch,
                    "agent": pr.agent,
                    "files": pr.files,
                },
            )
            await self._link_suspects_for_active_incidents(project_id)

    async def _link_suspects_for_active_incidents(self, project_id: str) -> None:
        settings = await get_monitoring_settings(self._databases, project_id)
        window_seconds = settings.recent_change_window_hours * 3600

        active_page = await self._incidents.list(
            project_id, status=list(ACTIVE_STATUSES), limit=100
        )
        if not active_page.rows:
            return

        all_changes = await self._store.list_recent_changes(project_id)
        if not all_changes:
            return

        for inc in active_page.rows:
            linked = await self._store.get_linked_suspects(project_id, inc.id)
            new_prs: set[int] = set(inc.prs)
            prs_changed = False

            for chg in all_changes:
                key = (str(chg["kind"]), str(chg["ref"]))
                if key in linked:
                    continue

                chg_at = parse_dt(str(chg["at"]))
                diff = (inc.started_at - chg_at).total_seconds()
                # Incident started within window after change, or change arrived during active incident
                if (0 <= diff <= window_seconds) or (-window_seconds <= diff < 0):
                    payload = {
                        "incident_id": inc.id,
                        "kind": chg["kind"],
                        "ref": chg["ref"],
                        "at": chg_at.isoformat(),
                        "detail": (
                            json.loads(str(chg["detail"]))
                            if isinstance(chg["detail"], str)
                            else chg["detail"]
                        ),
                    }
                    await self._events.record(
                        project_id,
                        source="system",
                        type="incident.suspect_linked",
                        payload=payload,
                        subject_id=inc.id,
                    )
                    linked.add(key)

                    if chg["kind"] in ("pr", "agent_pr"):
                        try:
                            pr_num = int(chg["ref"])
                            if pr_num not in new_prs:
                                new_prs.add(pr_num)
                                prs_changed = True
                        except ValueError:
                            pass

            if prs_changed:
                await self._store.update_incident_prs(project_id, inc.id, sorted(new_prs))
