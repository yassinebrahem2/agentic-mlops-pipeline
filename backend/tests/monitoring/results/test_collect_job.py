"""Tests for CollectResultsJob (Task 4 - issue #460)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.results.collect_job import CollectResultsJob
from mlops_agents.monitoring.results.contract import CollectionReport
from mlops_agents.monitoring.settings import set_monitoring_settings
from mlops_agents.storage.databases import Databases


class FakeResultsForJob:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        self.calls.append((project_id, now))
        return CollectionReport(stored=0, by_source={}, failures={})


async def test_collect_results_job_contract(databases: Databases, project_id: str) -> None:
    fake_results: Any = FakeResultsForJob()
    job = CollectResultsJob(databases, fake_results)

    assert hasattr(job, "due")
    assert hasattr(job, "run")
    assert job.kind == JobKind.COLLECT_RESULTS


async def test_collect_results_job_due_times_on_interval_grid(
    databases: Databases, project_id: str
) -> None:
    # Set collection_interval_s to 300 (5 minutes)
    await set_monitoring_settings(databases, project_id, {"collection_interval_s": 300})

    fake_results: Any = FakeResultsForJob()
    job = CollectResultsJob(databases, fake_results)

    after = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    now = datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)

    dues = await job.due(project_id, after, now)
    # interval_dues returns only the latest point of (after, now]
    assert len(dues) == 1
    assert all(isinstance(d, JobDue) for d in dues)
    assert all(d.kind == JobKind.COLLECT_RESULTS for d in dues)
    assert dues[0].due == datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)


async def test_collect_results_job_run_calls_collect(databases: Databases, project_id: str) -> None:
    fake_results = FakeResultsForJob()
    job = CollectResultsJob(databases, fake_results)

    due_time = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
    job_due = JobDue(kind=JobKind.COLLECT_RESULTS, due=due_time, subject="")

    await job.run(project_id, job_due)
    assert len(fake_results.calls) == 1
    assert fake_results.calls[0] == (project_id, due_time)
