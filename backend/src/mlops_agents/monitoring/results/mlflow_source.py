"""MLflow source reader for monitoring results (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.monitoring.results.contract import (
    ResultCategory,
    ResultRecord,
    parse_source_id,
)
from mlops_agents.monitoring.results.file_source import SourceReadOutcome
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import MlflowResults
from mlops_agents.tracking.contract import (
    MetricPoint,
    ModelVersionRow,
    RunRow,
    Tracking,
    TrackingQuery,
)

logger = logging.getLogger(__name__)


class MlflowSourceReader:
    """Reads metric points from MLflow runs via TrackingService."""

    def __init__(self, tracking: Tracking, handoffs: Handoffs) -> None:
        self._tracking = tracking
        self._handoffs = handoffs

    async def read(
        self, project_id: str, source_id: str, cursor: dict[str, Any]
    ) -> SourceReadOutcome:
        """Reads metric points for the metric named in source_id."""
        try:
            kind, metric_name, _ = parse_source_id(source_id)
        except ValueError as exc:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=1,
                last_error=scrub_url_secrets(str(exc)),
            )

        if kind != "mlflow":
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=1,
                last_error=f"Expected mlflow source_id, got: {source_id!r}",
            )

        threshold: float | None = None
        fires_when: Literal["above", "below"] | None = None
        category: ResultCategory = "model_performance"
        check_id: str = source_id

        try:
            handoff = await self._handoffs.current(project_id)
            if handoff is not None:
                ops = handoff.payload.operations
                if (
                    isinstance(ops.results_location, MlflowResults)
                    and ops.results_location.metric == metric_name
                ):
                    threshold = ops.results_location.threshold
                    fires_when = ops.results_location.fires_when
                for check in ops.checks:
                    if (
                        isinstance(check.results, MlflowResults)
                        and check.results.metric == metric_name
                    ):
                        threshold = check.results.threshold
                        fires_when = check.results.fires_when
                        category = check.category
                        check_id = check.check_id
                        break
        except Exception:
            logger.exception("Failed to inspect handoff for MLflow metric %s", metric_name)

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
                consecutive_failures=1,
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
                    if threshold is not None and fires_when is not None:
                        if fires_when == "above":
                            fired = point.value > threshold
                        elif fires_when == "below":
                            fired = point.value < threshold

                    rec = ResultRecord(
                        check_id=check_id,
                        origin="tracker",
                        ran_at=point.time,
                        fired=fired,
                        value=point.value,
                        threshold=threshold,
                        category=category,
                        subject=metric_name,
                        model=run_to_model.get(run.key),
                        detail={
                            "run_key": run.key,
                            "step": point.step,
                            "metric": point.metric,
                            "value": point.value,
                        },
                        source_ref=f"run:{run.key}",
                    )
                    source_key = f"{run.key}:{point.metric}:{point.step}:{point.time.isoformat()}"
                    records.append((source_key, rec))
            except Exception as exc:
                err = scrub_url_secrets(str(exc))
                logger.warning("Error querying metric %s for run %s: %s", metric_name, run.key, err)

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
