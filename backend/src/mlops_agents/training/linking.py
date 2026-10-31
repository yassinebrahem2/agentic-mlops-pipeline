"""RequestLinker implementation (Task 5, #490).

Links completed training runs back to their training requests by REQUEST_TAG,
retires expectations, marks fixes done, records training.linked, and wakes Retraining.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mlops_agents.monitoring.expectations.contract import ExpectationKey, Expectations
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.tracking.contract import RunRow, Tracking, TrackingQuery
from mlops_agents.training.constants import REQUEST_TAG, RETRAINING_AGENT
from mlops_agents.training.contract import TrainingWindow

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.monitoring.hooks.contract import Hooks, HookVerifier
    from mlops_agents.monitoring.incidents.contract import AgentWaker, Incidents
    from mlops_agents.training.fixes import SqliteFixes
    from mlops_agents.training.requests import SqliteTraining


class RequestLinker(SyncObserver):
    """SyncObserver that links runs back to training requests."""

    def __init__(
        self,
        training: SqliteTraining,
        fixes: SqliteFixes,
        tracking: Tracking,
        expectations: Expectations,
        events: EventLog,
        waker: AgentWaker,
        *,
        hooks: Hooks | None = None,
        incidents: Incidents | None = None,
        verifier: HookVerifier | None = None,
    ) -> None:
        self._training = training
        self._fixes = fixes
        self._tracking = tracking
        self._expectations = expectations
        self._events = events
        self._waker = waker
        self._hooks = hooks
        self._incidents = incidents
        self._verifier = verifier

    async def synced(self, project_id: str, now: datetime | None = None) -> None:
        started_reqs = await self._training.started_requests(project_id)
        if not started_reqs:
            return

        states = await self._expectations.states(project_id)
        silent_refs = {
            s.key.ref for s in states if s.key.kind == "training_request" and s.status == "silent"
        }

        for req in started_reqs:
            if req.id in silent_refs:
                await self._training.mark_silent(project_id, req.id)
                if req.fix_id is not None:
                    await self._fixes.set_outcome(project_id, req.fix_id, "not_cleared")

        active_reqs = [r for r in started_reqs if r.id not in silent_refs]
        if not active_reqs:
            return

        started_by_id = {r.id: r for r in active_reqs}
        oldest = min(r.started for r in active_reqs)

        offset = 0
        limit = 100
        while started_by_id:
            page = await self._tracking.query(
                project_id,
                TrackingQuery(what="runs", since=oldest, limit=limit, offset=offset),
            )
            for item in page.rows:
                if not isinstance(item, RunRow):
                    continue
                tag_req_id = item.tags.get(REQUEST_TAG)
                if tag_req_id and tag_req_id in started_by_id:
                    req = started_by_id.pop(tag_req_id)
                    ci_run_url: str | None = getattr(item, "ci_run_url", None)
                    await self._training.link(
                        project_id, req.id, run_key=item.key, ci_run_url=ci_run_url
                    )
                    await self._expectations.retire(
                        project_id,
                        ExpectationKey(kind="training_request", ref=req.id, model=req.model),
                    )

                    now_dt = now or datetime.now(UTC)
                    if self._hooks is not None and req.hook_id:
                        hook = await self._hooks.get(project_id, req.hook_id)
                        if (
                            hook is not None
                            and not hook.spec.deploys
                            and self._verifier is not None
                        ):
                            check_window = req.window or TrainingWindow(
                                start=req.started, end=now_dt
                            )
                            alias_check = await self._verifier.check_run(
                                project_id, hook.id, req.id, check_window
                            )
                            if alias_check.moved:
                                await self._hooks.suspend(project_id, hook.id, alias_check.detail)
                                if self._incidents is not None:
                                    draft = SignalDraft(
                                        kind="fired_result",
                                        category="operational",
                                        subject=f"hook:{hook.id}",
                                        model=req.model,
                                        ran_at=now_dt,
                                        detail={
                                            "reason": alias_check.detail,
                                            "hook_id": hook.id,
                                            "run_key": item.key,
                                        },
                                    )
                                    outcome = await self._incidents.raise_signal(
                                        project_id, draft, seen_at=now_dt
                                    )
                                    await self._incidents.set_severity(
                                        project_id,
                                        outcome.incident_id,
                                        severity="high",
                                        reason="deploys: false run moved production alias",
                                        by="system",
                                    )
                            else:
                                await self._hooks.set_deploys_verified(project_id, hook.id, True)
                    if req.fix_id is not None:
                        fix = await self._fixes.get(project_id, req.fix_id)
                        await self._fixes.mark_done(project_id, req.fix_id)
                        subject_id = fix.incident_id if fix is not None else req.fix_id
                        await self._events.record(
                            project_id,
                            source="training",
                            type="training.linked",
                            payload={
                                "request_id": req.id,
                                "run_key": item.key,
                                "fix_id": req.fix_id,
                            },
                            subject_id=subject_id,
                        )
                        await self._waker.wake(
                            project_id,
                            agent=RETRAINING_AGENT,
                            label="fix_request",
                            text=f"Training run linked for fix {req.fix_id}",
                            subject_id=subject_id,
                            key=f"linked:{req.id}",
                        )

            if not page.truncated or not page.rows or not started_by_id:
                break
            offset += len(page.rows)
