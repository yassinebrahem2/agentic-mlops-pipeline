"""ExpectationsService implementation and silence evaluation (I25)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from mlops_agents.clock import to_iso
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, Connections
from mlops_agents.monitoring.expectations.contract import (
    SECOND_DEADLINE_KEY,
    ExpectationKey,
    Expectations,
    ExpectationSpec,
    ExpectationState,
    Maintenance,
    NeverSuspended,
)
from mlops_agents.monitoring.expectations.keys import keys_for_result
from mlops_agents.monitoring.expectations.seed import HandoffExpectationsSeeder
from mlops_agents.monitoring.expectations.store import SqliteExpectationsStore
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.contract import ResultsObserver, StoredResult
from mlops_agents.monitoring.scheduler.times import next_occurrences
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.proposals.contract import HandoffObserver, Handoffs, HandoffView
from mlops_agents.proposals.payloads import DataFeedRef, JobFeedRef
from mlops_agents.providers.failover import Clock, SystemClock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import results
from mlops_agents.tracking.contract import TrackingQuery

logger = logging.getLogger(__name__)


def _scan_feed_files_sync(
    root_str: str, rel_files: list[str], secret_paths: set[str]
) -> float | None:
    """Synchronous file scanner for local folder connections (ASYNC240 compliant)."""
    root = Path(root_str)
    if not root.is_dir():
        return None

    max_mtime: float | None = None
    for rel_file in rel_files:
        if rel_file in secret_paths:
            continue
        candidate = (root / rel_file).resolve()
        try:
            if not candidate.is_relative_to(root) or not candidate.is_file():
                continue
            mtime = candidate.stat().st_mtime
            if max_mtime is None or mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            continue
    return max_mtime


class SignalSink(Protocol):
    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome: ...


class TrackingProtocol(Protocol):
    async def query(self, project_id: str, q: TrackingQuery) -> Any: ...


class _HandoffObserverWrapper(HandoffObserver):
    def __init__(self, service: ExpectationsService) -> None:
        self._service = service

    async def stored(self, project_id: str, handoff: HandoffView) -> None:
        await self._service.seed(project_id, handoff.payload)


class _ResultsObserverWrapper(ResultsObserver):
    def __init__(self, service: ExpectationsService) -> None:
        self._service = service

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        for res in results:
            for cand_key in keys_for_result(res):
                await self._service.note_seen(project_id, cand_key, res.ran_at)


class ExpectationsService(Expectations):
    """Expectations management, handoff seeding, seen updates, and silence evaluation."""

    def __init__(
        self,
        databases: Databases,
        handoffs: Handoffs,
        connections: Connections,
        incidents: SignalSink,
        *,
        tracking: TrackingProtocol | None = None,
        maintenance: Maintenance | None = None,
        clock: Clock | None = None,
        store: SqliteExpectationsStore | None = None,
        seeder: HandoffExpectationsSeeder | None = None,
    ) -> None:
        self._databases = databases
        self._handoffs = handoffs
        self._connections = connections
        self._incidents = incidents
        self._tracking = tracking
        self._maintenance = maintenance or NeverSuspended()
        self._clock = clock or SystemClock()
        self._store = store or SqliteExpectationsStore(databases, self._clock)
        self._seeder = seeder or HandoffExpectationsSeeder(self._store)

        self._handoff_observer = _HandoffObserverWrapper(self)
        self._results_observer = _ResultsObserverWrapper(self)

    @property
    def handoff_observer(self) -> HandoffObserver:
        return self._handoff_observer

    @property
    def results_observer(self) -> ResultsObserver:
        return self._results_observer

    @property
    def store(self) -> SqliteExpectationsStore:
        return self._store

    async def seed(self, project_id: str, payload: Any) -> None:
        await self._seeder.seed_from_payload(project_id, payload)

    async def reconcile(self, project_id: str) -> None:
        """Startup reconciliation of expectations from current handoff."""
        handoff = await self._handoffs.current(project_id)
        if handoff is not None:
            await self._seeder.seed_from_payload(project_id, handoff.payload)

    async def register(
        self, project_id: str, spec: ExpectationSpec, active: bool | None = None
    ) -> None:
        await self._store.register(project_id, spec, active=active)

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        await self._store.retire(project_id, key)

    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None:
        await self._store.note_seen(project_id, key, ran_at)

    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]:
        return await self._store.activate_for_pr(project_id, pr_number)

    async def _update_feeds_and_jobs_seen(self, project_id: str) -> None:
        """Updates last_seen for data feeds (from table files) and CI job feeds (from ci_runs)."""
        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            return

        ops = handoff.payload.operations
        # 1. Active data feeds
        active_expectations = await self._store.list_all(project_id, active_only=True)
        feed_specs = [spec for spec, *_ in active_expectations if spec.key.kind == "feed"]

        if feed_specs:
            try:
                conn_views = await self._connections.list(project_id)
                conn_map = {c.name: c for c in conn_views}
                conn_map.update({c.id: c for c in conn_views})

                for spec in feed_specs:
                    feed = next((f for f in ops.feeds if f.name == spec.key.ref), None)
                    if feed is None or not isinstance(feed.ref, DataFeedRef):
                        continue

                    conn_view = conn_map.get(feed.ref.connection)
                    if conn_view is None or conn_view.kind != LOCAL_FOLDER_KIND:
                        continue

                    tables = await self._connections.tables(project_id, conn_view.id)
                    table_ref = next((t for t in tables if t.name == feed.ref.table), None)
                    if table_ref is None:
                        continue

                    secret_paths = {f.path for f in conn_view.findings.secret_files}
                    path_str = str(conn_view.settings.get("path", ""))

                    max_mtime = await asyncio.to_thread(
                        _scan_feed_files_sync, path_str, table_ref.files, secret_paths
                    )

                    if max_mtime is not None:
                        newest_dt = datetime.fromtimestamp(max_mtime, tz=UTC)
                        await self._store.note_seen(project_id, spec.key, newest_dt)
            except Exception:
                logger.exception("Failed to update seen times for feeds in project %s", project_id)

        # 2. Active CI job feeds
        if self._tracking is not None:
            job_specs = [spec for spec, *_ in active_expectations if spec.key.kind == "job"]
            if job_specs:
                try:
                    ci_page = await self._tracking.query(
                        project_id, TrackingQuery(what="ci_runs", limit=200)
                    )
                    ci_runs = list(ci_page.rows)
                    for spec in job_specs:
                        feed = next((f for f in ops.feeds if f.name == spec.key.ref), None)
                        if feed is None or not isinstance(feed.ref, JobFeedRef):
                            continue
                        if not feed.ref.ci_workflow:
                            continue

                        wf_name = feed.ref.ci_workflow
                        matching_run = next((r for r in ci_runs if r.workflow == wf_name), None)
                        if matching_run is not None:
                            await self._store.note_seen(project_id, spec.key, matching_run.start)
                except Exception:
                    logger.exception(
                        "Failed to update seen times for CI jobs in project %s", project_id
                    )

    async def _check_result_exists(
        self,
        project_id: str,
        check_id: str,
        model: str | None,
        after: datetime,
        until: datetime,
    ) -> bool:
        """Checks if a result was recorded in results table in (after, until]."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(results.c.id).where(
                results.c.check_id == check_id,
                results.c.ran_at > to_iso(after),
                results.c.ran_at <= to_iso(until),
            )
            if model is not None:
                stmt = stmt.where(results.c.model == model)
            row = (await conn.execute(stmt.limit(1))).first()
            return row is not None

    async def evaluate(self, project_id: str, now: datetime) -> list[str]:
        """Evaluates expectations against now and raises silence signals for unmet deadlines.

        Returns list of signal IDs raised.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_utc = now.astimezone(UTC)

        settings = await get_monitoring_settings(self._databases, project_id)
        await self._update_feeds_and_jobs_seen(project_id)

        active_expectations = await self._store.list_all(project_id, active_only=True)
        signal_ids: list[str] = []

        second_deadline_delta = timedelta(minutes=settings.second_deadline_minutes)

        for (
            spec,
            _,
            last_seen,
            last_signalled_first,
            last_signalled_second,
            registered_at,
        ) in active_expectations:
            grace_mins = (
                spec.grace_minutes
                if spec.grace_minutes is not None
                else settings.check_grace_minutes
            )
            grace = timedelta(minutes=grace_mins)

            # Case A: due_at (single occurrence)
            if spec.due_at is not None:
                t = (
                    spec.due_at.replace(tzinfo=UTC)
                    if spec.due_at.tzinfo is None
                    else spec.due_at.astimezone(UTC)
                )
                if t < registered_at:
                    continue
                first_deadline = t + grace
                met = bool(last_seen is not None and last_seen <= first_deadline)

                if now_utc > first_deadline and not met:
                    if last_signalled_first is None:
                        is_suspended = await self._maintenance.suspended(project_id, spec.key, t)
                        if not is_suspended:
                            draft = SignalDraft(
                                kind="silence",
                                category=spec.category,
                                subject=spec.key.ref,
                                model=spec.key.model,
                                ran_at=t,
                                detail={
                                    "key": spec.key.model_dump(),
                                    "occurrence": t.isoformat(),
                                    "last_seen": to_iso(last_seen) if last_seen else None,
                                },
                                expectation=spec.key,
                            )
                            outcome = await self._incidents.raise_signal(project_id, draft, now_utc)
                            signal_ids.append(outcome.signal_id)
                        await self._store.update_signalled(project_id, spec.key, first_t=t)
                        last_signalled_first = t

                    if spec.second_deadline:
                        second_deadline = first_deadline + second_deadline_delta
                        still_unmet = not met and (last_seen is None or last_seen < t)
                        if (
                            now_utc > second_deadline
                            and still_unmet
                            and last_signalled_second is None
                        ):
                            is_suspended = await self._maintenance.suspended(
                                project_id, spec.key, t
                            )
                            if not is_suspended:
                                draft = SignalDraft(
                                    kind="silence",
                                    category=spec.category,
                                    subject=spec.key.ref,
                                    model=spec.key.model,
                                    ran_at=t,
                                    detail={
                                        "key": spec.key.model_dump(),
                                        "occurrence": t.isoformat(),
                                        "last_seen": to_iso(last_seen) if last_seen else None,
                                        SECOND_DEADLINE_KEY: True,
                                    },
                                    expectation=spec.key,
                                )
                                outcome = await self._incidents.raise_signal(
                                    project_id, draft, now_utc
                                )
                                signal_ids.append(outcome.signal_id)
                            await self._store.update_signalled(project_id, spec.key, second_t=t)
                            last_signalled_second = t
                continue

            # Case B: Cadence (recurring occurrences)
            if spec.cadence is None:
                continue

            # Anchors for first and second deadline evaluations
            first_candidates: list[datetime] = []
            if last_signalled_first is not None:
                first_candidates.append(last_signalled_first)
            if last_seen is not None:
                first_candidates.append(last_seen - grace)
            if first_candidates:
                first_anchor = max(first_candidates)
            else:
                first_anchor = now_utc - grace - timedelta(minutes=settings.check_grace_minutes)

            second_anchor: datetime | None = None
            if spec.second_deadline:
                second_candidates: list[datetime] = []
                if last_signalled_second is not None:
                    second_candidates.append(last_signalled_second)
                if last_seen is not None:
                    second_candidates.append(last_seen - grace - second_deadline_delta)
                if second_candidates:
                    second_anchor = max(second_candidates)
                else:
                    second_anchor = (
                        now_utc
                        - grace
                        - second_deadline_delta
                        - timedelta(minutes=settings.check_grace_minutes)
                    )

            earliest_anchor = first_anchor
            if second_anchor is not None and second_anchor < earliest_anchor:
                earliest_anchor = second_anchor

            # Lookback extra 35 days to get prev_occ for earliest candidate
            lookback_anchor = earliest_anchor - timedelta(days=35)
            full_occurrences = next_occurrences(
                spec.cadence,
                spec.timezone,
                after=lookback_anchor,
                until=now_utc,
            )

            for i, t in enumerate(full_occurrences):
                # A slot from before the expectation existed is never judged (#586).
                if t <= earliest_anchor or t < registered_at:
                    continue

                prev_occ = full_occurrences[i - 1] if i > 0 else (t - timedelta(hours=1))
                first_deadline = t + grace

                # Occurrence t is met if a result/seen falls in (prev_occ, first_deadline]
                met = False
                if last_seen is not None and prev_occ < last_seen <= first_deadline:
                    met = True
                elif spec.key.kind == "check":
                    met = await self._check_result_exists(
                        project_id, spec.key.ref, spec.key.model, prev_occ, first_deadline
                    )

                # 1. First deadline evaluation
                if (
                    t > first_anchor
                    and now_utc > first_deadline
                    and not met
                    and (last_signalled_first is None or t > last_signalled_first)
                ):
                    is_suspended = await self._maintenance.suspended(project_id, spec.key, t)
                    if not is_suspended:
                        draft = SignalDraft(
                            kind="silence",
                            category=spec.category,
                            subject=spec.key.ref,
                            model=spec.key.model,
                            ran_at=t,
                            detail={
                                "key": spec.key.model_dump(),
                                "occurrence": t.isoformat(),
                                "last_seen": to_iso(last_seen) if last_seen else None,
                            },
                            expectation=spec.key,
                        )
                        outcome = await self._incidents.raise_signal(project_id, draft, now_utc)
                        signal_ids.append(outcome.signal_id)
                    await self._store.update_signalled(project_id, spec.key, first_t=t)
                    last_signalled_first = t

                # 2. Second deadline evaluation
                if spec.second_deadline and second_anchor is not None and t > second_anchor:
                    second_deadline = first_deadline + second_deadline_delta
                    still_unmet = not met and (last_seen is None or last_seen < t)
                    if (
                        now_utc > second_deadline
                        and still_unmet
                        and (last_signalled_second is None or t > last_signalled_second)
                    ):
                        is_suspended = await self._maintenance.suspended(project_id, spec.key, t)
                        if not is_suspended:
                            draft = SignalDraft(
                                kind="silence",
                                category=spec.category,
                                subject=spec.key.ref,
                                model=spec.key.model,
                                ran_at=t,
                                detail={
                                    "key": spec.key.model_dump(),
                                    "occurrence": t.isoformat(),
                                    "last_seen": to_iso(last_seen) if last_seen else None,
                                    SECOND_DEADLINE_KEY: True,
                                },
                                expectation=spec.key,
                            )
                            outcome = await self._incidents.raise_signal(project_id, draft, now_utc)
                            signal_ids.append(outcome.signal_id)
                        await self._store.update_signalled(project_id, spec.key, second_t=t)
                        last_signalled_second = t

        return signal_ids

    async def states(self, project_id: str) -> list[ExpectationState]:
        """Returns the current state for each registered expectation."""
        now_utc = self._clock.now().astimezone(UTC)
        settings = await get_monitoring_settings(self._databases, project_id)
        all_records = await self._store.list_all(project_id, active_only=False)

        states_list: list[ExpectationState] = []

        for spec, active, last_seen, last_sig_first, _, _ in all_records:
            grace_mins = (
                spec.grace_minutes
                if spec.grace_minutes is not None
                else settings.check_grace_minutes
            )
            grace = timedelta(minutes=grace_mins)

            if not active:
                states_list.append(
                    ExpectationState(
                        key=spec.key,
                        category=spec.category,
                        active=False,
                        next_deadline=None,
                        last_seen=last_seen,
                        status="pending_pr",
                        since=None,
                    )
                )
                continue

            is_suspended = await self._maintenance.suspended(project_id, spec.key, now_utc)
            if is_suspended:
                states_list.append(
                    ExpectationState(
                        key=spec.key,
                        category=spec.category,
                        active=True,
                        next_deadline=None,
                        last_seen=last_seen,
                        status="suspended",
                        since=None,
                    )
                )
                continue

            # Check if currently silent
            if last_sig_first is not None and (last_seen is None or last_seen < last_sig_first):
                states_list.append(
                    ExpectationState(
                        key=spec.key,
                        category=spec.category,
                        active=True,
                        next_deadline=None,
                        last_seen=last_seen,
                        status="silent",
                        since=last_sig_first,
                    )
                )
                continue

            # Single due_at
            if spec.due_at is not None:
                t = spec.due_at.astimezone(UTC)
                first_deadline = t + grace
                if t <= now_utc <= first_deadline:
                    st = "late"
                    since_dt = t
                elif t - grace <= now_utc < t:
                    st = "due"
                    since_dt = t - grace
                elif now_utc > first_deadline:
                    st = "silent" if last_seen is None or last_seen < t else "ok"
                    since_dt = t if st == "silent" else last_seen
                else:
                    st = "ok"
                    since_dt = last_seen

                states_list.append(
                    ExpectationState(
                        key=spec.key,
                        category=spec.category,
                        active=True,
                        next_deadline=t,
                        last_seen=last_seen,
                        status=st,
                        since=since_dt,
                    )
                )
                continue

            # Recurring cadence
            if spec.cadence is not None:
                # Next upcoming occurrence
                future_occs = next_occurrences(
                    spec.cadence,
                    spec.timezone,
                    after=now_utc,
                    until=now_utc + timedelta(days=30),
                )
                next_deadline = future_occs[0] if future_occs else None

                # Recent occurrences that might be late
                recent_occs = next_occurrences(
                    spec.cadence,
                    spec.timezone,
                    after=now_utc - grace - timedelta(days=1),
                    until=now_utc,
                )

                st = "ok"
                since_dt = last_seen
                if recent_occs:
                    t_recent = recent_occs[-1]
                    if t_recent <= now_utc <= t_recent + grace:
                        prev_occ = (
                            recent_occs[-2]
                            if len(recent_occs) > 1
                            else t_recent - timedelta(hours=1)
                        )
                        met = bool(
                            last_seen is not None and prev_occ < last_seen <= t_recent + grace
                        )
                        if not met:
                            st = "late"
                            since_dt = t_recent
                            next_deadline = t_recent

                if (
                    st == "ok"
                    and next_deadline is not None
                    and (next_deadline - grace <= now_utc < next_deadline)
                ):
                    st = "due"
                    since_dt = next_deadline - grace

                states_list.append(
                    ExpectationState(
                        key=spec.key,
                        category=spec.category,
                        active=True,
                        next_deadline=next_deadline,
                        last_seen=last_seen,
                        status=st,
                        since=since_dt,
                    )
                )

        return states_list
