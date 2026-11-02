"""Follow-ups scheduled job (I33)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.storage.tables import incident_follow_ups, incidents

if TYPE_CHECKING:
    from mlops_agents.connections.contract import Connections
    from mlops_agents.data.summaries import DataSummaries
    from mlops_agents.monitoring.incidents.contract import AgentWaker
    from mlops_agents.proposals.contract import Handoffs
    from mlops_agents.providers.failover import Clock
    from mlops_agents.storage.databases import Databases


_logger = logging.getLogger(__name__)


class FollowUpsJob(ScheduledJob):
    """Scheduled job to check and wake agents for incident follow-ups."""

    kind: JobKind = JobKind.FOLLOW_UPS
    interval_s: int = 60

    def __init__(
        self,
        databases: Databases,
        waker: AgentWaker,
        handoffs: Handoffs,
        data_summaries: DataSummaries,
        connections: Connections,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._waker = waker
        self._handoffs = handoffs
        self._data_summaries = data_summaries
        self._connections = connections
        self._clock = clock

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        """Gives one due per collection interval."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)

        settings = await get_monitoring_settings(self._databases, project_id)
        due_times = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=dt, subject="") for dt in due_times]

    async def run(self, project_id: str, job: JobDue) -> None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(incident_follow_ups)
                .where(incident_follow_ups.c.done == 0)
                .order_by(incident_follow_ups.c.id.asc())
            )
            rows = [dict(r) for r in res.mappings().all()]

        if not rows:
            return

        now = self._clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        for row in rows:
            fol_id = str(row["id"])
            inc_id = str(row["incident"])
            agent = str(row["agent"])
            note = str(row["note"])
            after_days = row["after_days"]
            after_labels = row["after_labels"]
            due_str = row["due"]

            triggered = False

            if after_days is not None and due_str:
                due_dt = datetime.fromisoformat(due_str)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=UTC)
                if now >= due_dt:
                    triggered = True

            elif after_labels is not None:
                base_count = row["label_base_count"] or 0
                target_count = base_count + after_labels

                async with engine.connect() as conn:
                    inc_res = await conn.execute(
                        select(incidents.c.model).where(incidents.c.id == inc_id)
                    )
                    inc_row = inc_res.mappings().first()
                    model_name = str(inc_row["model"]) if inc_row and inc_row["model"] else None

                if model_name:
                    handoff = await self._handoffs.current(project_id)
                    if handoff is not None:
                        model_spec = next(
                            (m for m in handoff.payload.models if m.name == model_name),
                            None,
                        )
                        if model_spec and model_spec.labels:
                            labels_use = model_spec.labels
                            try:
                                refs = await self._connections.tables(
                                    project_id, labels_use.connection
                                )
                                ref = next((r for r in refs if r.name == labels_use.table), None)
                                if ref is not None:
                                    profile = await self._data_summaries.profile(
                                        project_id, labels_use.connection, ref
                                    )
                                    if (
                                        profile.row_count is not None
                                        and profile.row_count >= target_count
                                    ):
                                        triggered = True
                            except Exception:
                                _logger.warning(
                                    "Failed to profile labels table for incident %s",
                                    inc_id,
                                    exc_info=True,
                                )

            if triggered:
                await self._waker.wake(
                    project_id=project_id,
                    agent=agent,
                    label="follow_up",
                    subject_id=inc_id,
                    text=f"Follow-up for incident {inc_id}: {untrusted(note, source='follow_up')}",
                    key=fol_id,
                )
                async with engine.begin() as conn:
                    await conn.execute(
                        update(incident_follow_ups)
                        .where(incident_follow_ups.c.id == fol_id)
                        .values(done=1)
                    )
