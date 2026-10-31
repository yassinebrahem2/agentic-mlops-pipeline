"""CI source reader for monitoring results (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.monitoring.results.contract import ResultRecord, parse_source_id
from mlops_agents.monitoring.results.file_source import SourceReadOutcome
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import CiResults
from mlops_agents.tracking.contract import CiRunRow, Tracking, TrackingQuery

logger = logging.getLogger(__name__)


class CiSourceReader:
    """Reads finished CI runs as standard results from TrackingService."""

    def __init__(self, tracking: Tracking, handoffs: Handoffs) -> None:
        self._tracking = tracking
        self._handoffs = handoffs

    async def read(
        self, project_id: str, source_id: str, cursor: dict[str, Any]
    ) -> SourceReadOutcome:
        """Reads new finished CI runs for the workflow named in source_id."""
        try:
            kind, workflow, _ = parse_source_id(source_id)
        except ValueError as exc:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=1,
                last_error=scrub_url_secrets(str(exc)),
            )

        if kind != "ci":
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=1,
                last_error=f"Expected ci source_id, got: {source_id!r}",
            )

        # Determine check_id from handoff if one names this workflow, else fallback to source_id
        check_id = source_id
        try:
            handoff = await self._handoffs.current(project_id)
            if handoff is not None:
                for check in handoff.payload.operations.checks:
                    if isinstance(check.results, CiResults) and check.results.workflow == workflow:
                        check_id = check.check_id
                        break
        except Exception:
            logger.exception("Failed to inspect handoff for CI check_id for %s", source_id)

        since_str = cursor.get("since")
        seen_keys: set[str] = set(cursor.get("seen_keys", []))
        since_dt = datetime.fromisoformat(since_str) if since_str else None

        try:
            # Query ci_runs from tracking
            all_ci_runs: list[CiRunRow] = []
            offset = 0
            limit = 500
            while True:
                page = await self._tracking.query(
                    project_id,
                    TrackingQuery(what="ci_runs", since=since_dt, limit=limit, offset=offset),
                )
                runs_page = [r for r in page.rows if isinstance(r, CiRunRow)]
                all_ci_runs.extend(runs_page)
                if len(page.rows) < limit:
                    break
                offset += limit
        except Exception as exc:
            err = scrub_url_secrets(str(exc))
            logger.warning("Error querying CI runs for %s: %s", source_id, err)
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=1,
                last_error=err,
            )

        matching_runs = [r for r in all_ci_runs if r.workflow == workflow]
        matching_runs.sort(key=lambda r: (r.start, r.key))

        records: list[tuple[str, ResultRecord]] = []
        new_seen_keys = set(seen_keys)
        new_since_str = since_str
        held = False

        for run in matching_runs:
            if run.key in seen_keys:
                continue

            if run.status == "running":
                # Running run holds cursor at its start; stop reading subsequent runs
                new_since_str = run.start.isoformat()
                held = True
                break

            fired = run.status in ("failed", "cancelled")
            ran_at = run.start + timedelta(seconds=run.duration_s or 0.0)
            source_key = run.key
            source_ref = run.url or run.key
            detail = {
                "status": run.status,
                "duration_s": run.duration_s,
                "commit": run.commit,
                "url": run.url,
                "workflow": run.workflow,
            }
            rec = ResultRecord(
                check_id=check_id,
                origin="tracker",
                ran_at=ran_at,
                fired=fired,
                category="operational",
                subject=run.workflow,
                model=None,
                detail=detail,
                source_ref=source_ref,
            )
            records.append((source_key, rec))
            new_seen_keys.add(run.key)
            new_since_str = run.start.isoformat()

        if not held and matching_runs and records:
            newest_start = max(r.start for r in matching_runs if r.key in new_seen_keys)
            new_since_str = newest_start.isoformat()
            new_seen_keys = {
                r.key for r in matching_runs if r.start == newest_start and r.key in new_seen_keys
            }
        elif held and new_since_str is not None:
            running_start = datetime.fromisoformat(new_since_str)
            new_seen_keys = {
                r.key for r in matching_runs if r.start >= running_start and r.key in new_seen_keys
            }

        new_cursor: dict[str, Any] = {}
        if new_since_str is not None:
            new_cursor["since"] = new_since_str
            new_cursor["seen_keys"] = sorted(new_seen_keys)
        else:
            new_cursor = dict(cursor)

        return SourceReadOutcome(
            records=records,
            cursor=new_cursor,
            consecutive_failures=0,
            last_error=None,
        )
