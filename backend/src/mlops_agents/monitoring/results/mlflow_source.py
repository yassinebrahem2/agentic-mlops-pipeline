"""MLflow source reader for monitoring results (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.monitoring.expectations.contract import CheckView
from mlops_agents.monitoring.results.contract import (
    ResultCategory,
    ResultOrigin,
    ResultRecord,
    parse_source_id,
)
from mlops_agents.monitoring.results.file_source import SourceReadOutcome
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import MlflowResults
from mlops_agents.tracking.contract import (
    MetricPoint,
    ModelVersionRow,
    RunRow,
    Tracking,
    TrackingQuery,
)

logger = logging.getLogger(__name__)


class RegisteredChecks(Protocol):
    """The read-only view of the checks store this source needs; the store owns the checks."""

    async def list_active(self, project_id: str) -> list[CheckView]: ...


@dataclass(frozen=True)
class MetricOwner:
    """Whose records an MLflow metric gives, and what makes them fire."""

    check_id: str
    origin: ResultOrigin
    category: ResultCategory
    version: str | None
    threshold: float | None
    fires_when: Literal["above", "below"] | None


def _handoff_owner(source_id: str, metric: str, handoff: HandoffView | None) -> MetricOwner | None:
    """The handoff's claim on a metric: its check naming it, else its results location."""
    if handoff is None:
        return None
    ops = handoff.payload.operations
    for check in ops.checks:
        if isinstance(check.results, MlflowResults) and check.results.metric == metric:
            return MetricOwner(
                check_id=check.check_id,
                origin="tracker",
                category=check.category,
                version=None,
                threshold=check.results.threshold,
                fires_when=check.results.fires_when,
            )
    location = ops.results_location
    if isinstance(location, MlflowResults) and location.metric == metric:
        return MetricOwner(
            check_id=source_id,
            origin="tracker",
            category="model_performance",
            version=None,
            threshold=location.threshold,
            fires_when=location.fires_when,
        )
    return None


def metric_owner(
    source_id: str,
    metric: str,
    active_checks: Sequence[CheckView],
    handoff: HandoffView | None,
) -> MetricOwner:
    """The one place that decides whose records an MLflow metric gives.

    An active registered check naming the metric wins, even over a later handoff naming it
    too (RegisterCheck refuses the other order); else the handoff decides, as before #628.
    """
    claimed = _handoff_owner(source_id, metric, handoff)
    for chk in active_checks:
        location = chk.results_location
        if isinstance(location, MlflowResults) and location.metric == metric:
            if claimed is not None:
                logger.warning(
                    "MLflow metric %s is named by the handoff (as %s) and by registered "
                    "check %s; the registered check's records win",
                    metric,
                    claimed.check_id,
                    chk.check_id,
                )
            return MetricOwner(
                check_id=chk.check_id,
                origin="agent",
                category=chk.category,
                version=chk.version,
                threshold=location.threshold,
                fires_when=location.fires_when,
            )
    if claimed is not None:
        return claimed
    return MetricOwner(
        check_id=source_id,
        origin="tracker",
        category="model_performance",
        version=None,
        threshold=None,
        fires_when=None,
    )


class MlflowSourceReader:
    """Reads metric points from MLflow runs via TrackingService."""

    def __init__(
        self,
        tracking: Tracking,
        handoffs: Handoffs,
        checks: RegisteredChecks | None = None,
    ) -> None:
        self._tracking = tracking
        self._handoffs = handoffs
        self._checks = checks

    def bind_checks(self, checks: RegisteredChecks) -> None:
        """Binds the registered checks, which are built after the results service."""
        self._checks = checks

    async def read(
        self,
        project_id: str,
        source_id: str,
        cursor: dict[str, Any],
        *,
        consecutive_failures: int = 0,
    ) -> SourceReadOutcome:
        """Reads metric points for the metric named in source_id."""
        try:
            kind, metric_name, _ = parse_source_id(source_id)
        except ValueError as exc:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=scrub_url_secrets(str(exc)),
            )

        if kind != "mlflow":
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=f"Expected mlflow source_id, got: {source_id!r}",
            )

        try:
            active_checks = (
                await self._checks.list_active(project_id) if self._checks is not None else []
            )
        except Exception as exc:
            # Records dedupe on their source key, so ones stored under the wrong check could
            # never be corrected: fail the read and retry from the same cursor instead.
            err = scrub_url_secrets(str(exc))
            logger.warning("Error listing registered checks for %s: %s", source_id, err)
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=err,
            )

        handoff: HandoffView | None = None
        try:
            handoff = await self._handoffs.current(project_id)
        except Exception:
            logger.exception("Failed to inspect handoff for MLflow metric %s", metric_name)
        owner = metric_owner(source_id, metric_name, active_checks, handoff)

        since_str = cursor.get("since")
        since_dt = datetime.fromisoformat(since_str) if since_str else None

        try:
            # Query versions to map run_key -> model
            v_page = await self._tracking.query(
                project_id, TrackingQuery(what="versions", limit=500)
            )
            run_to_model: dict[str, str] = {
                v.run_key: v.model
                for v in v_page.rows
                if isinstance(v, ModelVersionRow) and v.run_key
            }

            # Query runs starting from cursor time
            all_runs: list[RunRow] = []
            offset = 0
            limit = 500
            while True:
                r_page = await self._tracking.query(
                    project_id,
                    TrackingQuery(what="runs", since=since_dt, limit=limit, offset=offset),
                )
                runs_page = [r for r in r_page.rows if isinstance(r, RunRow)]
                all_runs.extend(runs_page)
                if len(r_page.rows) < limit:
                    break
                offset += limit
        except Exception as exc:
            err = scrub_url_secrets(str(exc))
            logger.warning("Error querying MLflow runs for %s: %s", source_id, err)
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=err,
            )

        if not all_runs:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=0,
                last_error=None,
            )

        all_runs.sort(key=lambda r: (r.start or datetime.min.replace(tzinfo=UTC), r.key))

        records: list[tuple[str, ResultRecord]] = []
        for run in all_runs:
            try:
                p_page = await self._tracking.query(
                    project_id,
                    TrackingQuery(what="metrics", run_key=run.key, metric=metric_name, limit=500),
                )
                for point in p_page.rows:
                    if not isinstance(point, MetricPoint):
                        continue

                    fired = False
                    if owner.threshold is not None and owner.fires_when is not None:
                        if owner.fires_when == "above":
                            fired = point.value > owner.threshold
                        elif owner.fires_when == "below":
                            fired = point.value < owner.threshold

                    rec = ResultRecord(
                        check_id=owner.check_id,
                        origin=owner.origin,
                        ran_at=point.time,
                        fired=fired,
                        value=point.value,
                        threshold=owner.threshold,
                        category=owner.category,
                        subject=metric_name,
                        model=run_to_model.get(run.key),
                        detail={
                            "run_key": run.key,
                            "step": point.step,
                            "metric": point.metric,
                            "value": point.value,
                        },
                        source_ref=f"run:{run.key}",
                        version=owner.version,
                    )
                    source_key = f"{run.key}:{point.metric}:{point.step}:{point.time.isoformat()}"
                    records.append((source_key, rec))
            except Exception as exc:
                err = scrub_url_secrets(str(exc))
                logger.warning("Error querying metric %s for run %s: %s", metric_name, run.key, err)
                # Stop before this run (keep input cursor) and report the failure
                return SourceReadOutcome(
                    records=records,
                    cursor=cursor,
                    consecutive_failures=consecutive_failures + 1,
                    last_error=err,
                )

        # Running runs hold cursor at their start; when all runs finished, cursor advances
        running_starts = [
            r.start for r in all_runs if r.status == "running" and r.start is not None
        ]
        if running_starts:
            earliest_running_start: datetime = min(running_starts)
            new_cursor: dict[str, Any] = {"since": earliest_running_start.isoformat()}
        else:
            runs_starts = [r.start for r in all_runs if r.start is not None]
            if runs_starts:
                newest_start: datetime = max(runs_starts)
                new_cursor = {"since": newest_start.isoformat()}
            else:
                new_cursor = dict(cursor)

        return SourceReadOutcome(
            records=records,
            cursor=new_cursor,
            consecutive_failures=0,
            last_error=None,
        )
