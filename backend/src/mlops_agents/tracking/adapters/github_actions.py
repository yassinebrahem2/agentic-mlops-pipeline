"""GitHub Actions tracking adapter (I16).

Syncs workflow runs from a GitHub repository via the GitHub REST API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Final

import httpx2

from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.actions import list_workflow_runs
from mlops_agents.tracking.contract import (
    CiRunRow,
    GithubActionsSettings,
    RunStatus,
    SourceType,
    SyncResult,
    TrackingAdapter,
    TrackingSourceView,
)

GITHUB_FIRST_SYNC_DAYS: Final[int] = 30
GITHUB_RUN_CAP: Final[int] = 300
GITHUB_OVERLAP: Final[timedelta] = timedelta(days=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _empty_result(error: str | None = None) -> SyncResult:
    return SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=error,
    )


class GithubActionsAdapter(TrackingAdapter):
    type: SourceType = "github_actions"

    def __init__(
        self,
        http: httpx2.AsyncClient,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._http = http
        self._clock = clock

    async def sync(
        self,
        source: TrackingSourceView,
        credential: str | None,
        since: datetime | None,
    ) -> SyncResult:
        if not credential:
            return _empty_result(error="Log in to GitHub to read CI runs")

        try:
            settings = GithubActionsSettings.model_validate(source.settings)
        except Exception as exc:
            return _empty_result(error=f"Invalid settings: {error_text(exc)}")

        if since is None:
            created_from = self._clock() - timedelta(days=GITHUB_FIRST_SYNC_DAYS)
        else:
            created_from = since - GITHUB_OVERLAP

        try:
            runs = await list_workflow_runs(
                self._http,
                credential,
                settings.repository,
                created_from=created_from,
                limit=GITHUB_RUN_CAP,
            )
        except DomainError as exc:
            return _empty_result(error=exc.message)
        except Exception as exc:
            return _empty_result(error=error_text(exc))

        ci_runs: list[CiRunRow] = []
        for run in runs:
            workflow_name = PurePosixPath(run.path).name
            if settings.workflows and workflow_name not in settings.workflows:
                continue

            gh_status = (run.status or "").lower()
            gh_conclusion = (run.conclusion or "").lower() if run.conclusion else None

            run_status: RunStatus
            if gh_status in ("queued", "in_progress", "waiting", "requested", "pending"):
                run_status = "running"
            elif gh_status == "completed":
                if gh_conclusion == "success":
                    run_status = "succeeded"
                elif gh_conclusion in ("failure", "timed_out", "startup_failure"):
                    run_status = "failed"
                elif gh_conclusion == "cancelled":
                    run_status = "cancelled"
                else:
                    run_status = "other"
            else:
                run_status = "other"

            start_dt = run.run_started_at if run.run_started_at is not None else run.created_at

            duration_s: float | None = None
            if gh_status == "completed":
                duration_s = max(0.0, (run.updated_at - start_dt).total_seconds())

            ci_runs.append(
                CiRunRow(
                    source_id=source.id,
                    key=str(run.id),
                    workflow=workflow_name,
                    status=run_status,
                    start=start_dt,
                    duration_s=duration_s,
                    commit=run.head_sha,
                    url=run.html_url,
                )
            )

        return SyncResult(
            versions=[],
            runs=[],
            metrics=[],
            aliases={},
            ci_runs=ci_runs,
            evaluations=[],
            error=None,
        )
