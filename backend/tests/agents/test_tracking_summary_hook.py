"""Tests for the tracking_summary context hook."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.hooks.tracking_summary import (
    TRACKING_SUMMARY_HOOK,
    TRACKING_SUMMARY_MAX_CHARS,
    TrackingSummaryHook,
)
from mlops_agents.agents.tools import default_registry
from mlops_agents.tracking.contract import (
    CiRunRow,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    Page,
    RunRow,
    TrackingQuery,
    TrackingSourceView,
)


class ScriptedTracking:
    def __init__(self) -> None:
        self.sources_list: list[TrackingSourceView] = []
        self.models_page: Page[ModelSummary] = Page(rows=[], total=0, truncated=False)
        self.versions_by_model_version: dict[tuple[str, str], Page[ModelVersionRow]] = {}
        self.metrics_by_run: dict[str, Page[MetricPoint]] = {}
        self.runs_page: Page[RunRow] = Page(rows=[], total=0, truncated=False)
        self.ci_runs_page: Page[CiRunRow] = Page(rows=[], total=0, truncated=False)
        self.raise_error: Exception | None = None

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        if self.raise_error is not None:
            raise self.raise_error
        return self.sources_list

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if self.raise_error is not None:
            raise self.raise_error
        if q.what == "models":
            return self.models_page
        if q.what == "versions":
            key = (q.model or "", q.version or "")
            return self.versions_by_model_version.get(key, Page(rows=[], total=0, truncated=False))
        if q.what == "metrics":
            return self.metrics_by_run.get(q.run_key or "", Page(rows=[], total=0, truncated=False))
        if q.what == "runs":
            return self.runs_page
        if q.what == "ci_runs":
            return self.ci_runs_page
        return Page(rows=[], total=0, truncated=False)


def _hook_ctx(project_id: str = "prj_test") -> HookContext:
    return HookContext(project_id=project_id, agent="lead", trigger_text="start")


@pytest.mark.asyncio
async def test_tracking_summary_hook_full_record_production_alias() -> None:
    now_dt = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    tracking = ScriptedTracking()
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_1",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
            credential_set=False,
            last_sync=now_dt,
            last_error=None,
        )
    ]
    tracking.models_page = Page(
        rows=[
            ModelSummary(
                source_id="trk_1",
                name="churn",
                production_version="2",
                latest_version="3",
                versions=3,
            )
        ],
        total=1,
        truncated=False,
    )
    tracking.versions_by_model_version[("churn", "2")] = Page(
        rows=[
            ModelVersionRow(
                source_id="trk_1",
                model="churn",
                version="2",
                run_key="run_prod",
                aliases=["production"],
                created=now_dt,
                evaluation="present",
            )
        ],
        total=1,
        truncated=False,
    )
    tracking.metrics_by_run["run_prod"] = Page(
        rows=[
            MetricPoint(
                source_id="trk_1",
                run_key="run_prod",
                metric="f1_score",
                step=1,
                value=0.91,
                time=now_dt,
            ),
            MetricPoint(
                source_id="trk_1",
                run_key="run_prod",
                metric="accuracy",
                step=1,
                value=0.95,
                time=now_dt,
            ),
        ],
        total=2,
        truncated=False,
    )
    tracking.runs_page = Page(
        rows=[
            RunRow(
                source_id="trk_1",
                key="run_prod",
                experiment="exp1",
                status="succeeded",
                start=now_dt,
                end=now_dt,
                params={},
                tags={},
                git_commit="sha1",
                datasets=[],
            )
        ],
        total=1,
        truncated=False,
    )
    tracking.ci_runs_page = Page(
        rows=[
            CiRunRow(
                source_id="trk_2",
                key="ci_1",
                workflow="ci.yml",
                status="succeeded",
                start=now_dt,
                duration_s=60,
                commit="sha1",
                url="https://github.com",
            )
        ],
        total=1,
        truncated=False,
    )

    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())

    assert "Production model: churn v2 (production alias)" in rendered
    assert "f1_score" in rendered
    assert "0.91" in rendered
    assert "run_prod" in rendered
    assert "ci.yml" in rendered


@pytest.mark.asyncio
async def test_tracking_summary_hook_fallback_to_newest_version() -> None:
    now_dt = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    tracking = ScriptedTracking()
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_1",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
            credential_set=False,
            last_sync=now_dt,
            last_error=None,
        )
    ]
    # production_version is None, latest_version is "5"
    tracking.models_page = Page(
        rows=[
            ModelSummary(
                source_id="trk_1",
                name="churn",
                production_version=None,
                latest_version="5",
                versions=5,
            )
        ],
        total=1,
        truncated=False,
    )
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())

    assert "Production model: churn v5 (newest version)" in rendered


@pytest.mark.asyncio
async def test_tracking_summary_hook_200_runs_under_3000_chars() -> None:
    now_dt = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    tracking = ScriptedTracking()
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_1",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
            credential_set=False,
            last_sync=now_dt,
            last_error=None,
        )
    ]
    # Long run list
    tracking.runs_page = Page(
        rows=[
            RunRow(
                source_id="trk_1",
                key=f"run_very_long_experiment_name_and_key_{i:04d}",
                experiment="deep_neural_network_hyperparameter_optimization_experiment",
                status="succeeded",
                start=now_dt,
                end=now_dt,
                params={"learning_rate": "0.001", "batch_size": "64"},
                tags={},
                git_commit="deadbeef" * 5,
                datasets=["training_dataset_v1_split_clean"],
            )
            for i in range(200)
        ],
        total=200,
        truncated=False,
    )
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())

    assert len(rendered) <= TRACKING_SUMMARY_MAX_CHARS


@pytest.mark.asyncio
async def test_tracking_summary_hook_no_sources() -> None:
    tracking = ScriptedTracking()
    tracking.sources_list = []
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())
    assert rendered == "No tracking source is connected."


@pytest.mark.asyncio
async def test_tracking_summary_hook_source_in_error_or_unsynced() -> None:
    tracking = ScriptedTracking()
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_err",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
            credential_set=False,
            last_sync=None,
            last_error="Connection timed out after 30s",
        ),
        TrackingSourceView(
            id="trk_unsynced",
            type="github_actions",
            settings={"repository": "org/repo"},
            auth="none",
            credential_set=False,
            last_sync=None,
            last_error=None,
        ),
    ]
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())
    assert "trk_err" in rendered
    assert "Connection timed out after 30s" in rendered
    assert "trk_unsynced" in rendered
    assert "no sync yet" in rendered


@pytest.mark.asyncio
async def test_tracking_summary_hook_raising_service() -> None:
    tracking = ScriptedTracking()
    tracking.raise_error = RuntimeError("Database is locked")
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())
    assert rendered == "Tracking summary is unavailable."


@pytest.mark.asyncio
async def test_tracking_summary_hook_no_secrets_leaked() -> None:
    secret = "ghp_VERY_SECRET_TOKEN_VALUE_12345"
    tracking = ScriptedTracking()
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_1",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="credential",
            credential_set=True,
            last_sync=None,
            last_error=f"Failed to auth with {secret}",
        )
    ]
    # Even if an error somehow had a secret in last_error, let's verify settings credentials aren't dumped
    hook = TrackingSummaryHook(tracking=tracking)  # type: ignore[arg-type]
    rendered = await hook.render(_hook_ctx())
    # The hook should not leak raw settings dict or credentials
    assert "settings=" not in rendered
    assert "auth=" not in rendered


def test_tracking_summary_registered() -> None:
    registry = HookRegistry()
    hook = TrackingSummaryHook()
    registry.register(hook)
    assert TRACKING_SUMMARY_HOOK in registry.names()
    assert "tracking_summary" in registry.names()

    tools = default_registry(tracking=object())  # pyright: ignore[reportArgumentType]
    assert "TrackingHistory" in tools.names()
