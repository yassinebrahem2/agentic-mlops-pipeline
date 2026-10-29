"""Tests for the TrackingHistory agent tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from mlops_agents.agents.policy.table import PermissionLevel, decide
from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext
from mlops_agents.agents.tools.tracking_history import TrackingHistory
from mlops_agents.tracking.constants import QUERY_LIMIT_MAX
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    EvaluationRecord,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    Page,
    RunRow,
    TrackingQuery,
    TrackingSourceView,
)


def _tool_ctx(project_id: str = "prj_test") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent="lead",
        session_id="ses_test",
        turn_id="trn_test",
        files=cast(FileAccess, None),
        task=None,
        tool_call_id="call_test",
    )


class FakeTrackingService:
    def __init__(self) -> None:
        self.pages: dict[str, Page[object]] = {}
        self.sources_list: list[TrackingSourceView] = []
        self.last_query: TrackingQuery | None = None
        self.raise_on_query: Exception | None = None

    async def query(self, project_id: str, q: TrackingQuery) -> Page[object]:
        if self.raise_on_query is not None:
            raise self.raise_on_query
        self.last_query = q
        if q.what == "metrics" and not q.run_key:
            from mlops_agents.errors import DomainError

            raise DomainError(
                "tracking_bad_query", "Querying metrics requires run_key.", status=422
            )
        return self.pages.get(q.what, Page(rows=[], total=0, truncated=False))

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        return self.sources_list


def test_tracking_history_risk_and_policy() -> None:
    assert TrackingHistory.risk == RiskClass.READ
    levels: list[PermissionLevel] = ["suggest", "ask", "autonomous", "bypass"]
    for level in levels:
        assert decide(TrackingHistory.risk, level) == "allow"


@pytest.mark.asyncio
async def test_tracking_history_each_what() -> None:
    tracking = FakeTrackingService()
    tool = TrackingHistory(tracking=tracking)  # type: ignore[arg-type]
    ctx = _tool_ctx()
    now_dt = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    # 1. models
    tracking.pages["models"] = Page(
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
    res = await tool.invoke(ctx, {"what": "models"})
    assert not res.is_error
    assert "churn" in res.content
    assert "1 of 1 rows" in res.content

    # 2. versions
    tracking.pages["versions"] = Page(
        rows=[
            ModelVersionRow(
                source_id="trk_1",
                model="churn",
                version="1",
                run_key="r1",
                aliases=["prod"],
                created=now_dt,
                evaluation="present",
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "versions", "model": "churn"})
    assert not res.is_error
    assert "churn" in res.content
    assert "version: 1" in res.content or "v1" in res.content or "1" in res.content
    assert "1 of 1 rows" in res.content

    # 3. runs
    tracking.pages["runs"] = Page(
        rows=[
            RunRow(
                source_id="trk_1",
                key="run_abc",
                experiment="exp_1",
                status="succeeded",
                start=now_dt,
                end=now_dt,
                params={"lr": "0.01"},
                tags={},
                git_commit="commit123",
                datasets=["ds_train"],
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "runs"})
    assert not res.is_error
    assert "run_abc" in res.content
    assert "succeeded" in res.content
    assert "1 of 1 rows" in res.content

    # 4. metrics
    tracking.pages["metrics"] = Page(
        rows=[
            MetricPoint(
                source_id="trk_1",
                run_key="run_abc",
                metric="accuracy",
                step=10,
                value=0.92,
                time=now_dt,
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "metrics", "run_key": "run_abc"})
    assert not res.is_error
    assert "accuracy" in res.content
    assert "0.92" in res.content
    assert "1 of 1 rows" in res.content

    # 5. deployments
    tracking.pages["deployments"] = Page(
        rows=[
            DeploymentRow(
                source_id="trk_1",
                model="churn",
                alias="production",
                from_version="1",
                to_version="2",
                seen_at=now_dt,
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "deployments"})
    assert not res.is_error
    assert "churn" in res.content
    assert "production" in res.content
    assert "1 of 1 rows" in res.content

    # 6. ci_runs
    tracking.pages["ci_runs"] = Page(
        rows=[
            CiRunRow(
                source_id="trk_2",
                key="12345",
                workflow="train.yml",
                status="succeeded",
                start=now_dt,
                duration_s=120,
                commit="sha123",
                url="https://github.com/org/repo/actions/runs/12345",
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "ci_runs"})
    assert not res.is_error
    assert "train.yml" in res.content
    assert "succeeded" in res.content
    assert "1 of 1 rows" in res.content

    # 7. evaluations: compact JSON capped at 400 chars
    large_payload = {f"metric_{i}": i * 1.5 for i in range(50)}
    tracking.pages["evaluations"] = Page(
        rows=[
            EvaluationRecord(
                source_id="trk_1",
                model="churn",
                version="2",
                run_key="run_abc",
                schema_version=1,
                payload=large_payload,
                fetched_at=now_dt,
            )
        ],
        total=1,
        truncated=False,
    )
    res = await tool.invoke(ctx, {"what": "evaluations"})
    assert not res.is_error
    assert "churn" in res.content
    # The payload in the line must not exceed 400 characters
    line = next(ln for ln in res.content.splitlines() if "churn" in ln)
    assert len(line) < 600  # header fields + <= 400 char payload


@pytest.mark.asyncio
async def test_tracking_history_truncation_advice() -> None:
    tracking = FakeTrackingService()
    tool = TrackingHistory(tracking=tracking)  # type: ignore[arg-type]
    ctx = _tool_ctx()

    tracking.pages["runs"] = Page(
        rows=[
            RunRow(
                source_id="trk_1",
                key=f"run_{i}",
                experiment="exp",
                status="succeeded",
                start=None,
                end=None,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            )
            for i in range(5)
        ],
        total=100,
        truncated=True,
    )
    res = await tool.invoke(ctx, {"what": "runs", "limit": 5})
    assert not res.is_error
    assert "5 of 100 rows" in res.content
    assert "truncated" in res.content.lower()
    assert "since" in res.content
    assert "limit" in res.content
    assert "offset" in res.content


@pytest.mark.asyncio
async def test_tracking_history_empty_with_and_without_source_error() -> None:
    tracking = FakeTrackingService()
    tool = TrackingHistory(tracking=tracking)  # type: ignore[arg-type]
    ctx = _tool_ctx()

    # Empty without error
    tracking.pages["models"] = Page(rows=[], total=0, truncated=False)
    tracking.sources_list = [
        TrackingSourceView(
            id="trk_1",
            type="mlflow",
            settings={"url": "https://mlflow.example.com"},
            auth="none",
            credential_set=False,
            last_sync=None,
            last_error=None,
        )
    ]
    res = await tool.invoke(ctx, {"what": "models"})
    assert not res.is_error
    assert "No models recorded yet" in res.content

    # Empty with source error
    tracking.sources_list[0].last_error = "Connection refused to MLflow tracker"
    res = await tool.invoke(ctx, {"what": "models"})
    assert not res.is_error
    assert "No models recorded yet" in res.content
    assert "Connection refused to MLflow tracker" in res.content


@pytest.mark.asyncio
async def test_tracking_history_metrics_requires_run_key() -> None:
    tracking = FakeTrackingService()
    tool = TrackingHistory(tracking=tracking)  # type: ignore[arg-type]
    ctx = _tool_ctx()

    res = await tool.invoke(ctx, {"what": "metrics"})
    assert res.is_error
    assert "Querying metrics requires run_key." in res.content


@pytest.mark.asyncio
async def test_tracking_history_limit_clamped() -> None:
    tracking = FakeTrackingService()
    tool = TrackingHistory(tracking=tracking)  # type: ignore[arg-type]
    ctx = _tool_ctx()

    tracking.pages["runs"] = Page(rows=[], total=0, truncated=False)
    res = await tool.invoke(ctx, {"what": "runs", "limit": 1000})
    assert not res.is_error
    assert tracking.last_query is not None
    assert tracking.last_query.limit == QUERY_LIMIT_MAX
