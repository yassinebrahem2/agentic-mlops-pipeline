"""Tests for CheckExpectationsJob."""

from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationState
from mlops_agents.monitoring.expectations.job import CheckExpectationsJob
from mlops_agents.monitoring.jobs import JobKind
from mlops_agents.monitoring.settings import MonitoringSettings, set_monitoring_settings
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock


class FakeExpectations:
    def __init__(self) -> None:
        self.evaluated: list[tuple[str, datetime]] = []

    async def register(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def retire(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def note_seen(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def activate_for_pr(self, *args: Any, **kwargs: Any) -> list[ExpectationKey]:
        return []

    async def evaluate(self, project_id: str, now: datetime) -> list[str]:
        self.evaluated.append((project_id, now))
        return []

    async def states(self, project_id: str) -> list[ExpectationState]:
        return []


@pytest.fixture
def project_id() -> str:
    return "prj_testjob"


async def test_check_expectations_job_due_and_run(databases: Databases, project_id: str) -> None:
    await set_monitoring_settings(
        databases, project_id, MonitoringSettings(collection_interval_s=60)
    )
    fake_exp = FakeExpectations()
    clock = FakeClock()
    job = CheckExpectationsJob(databases, fake_exp, clock=clock)

    assert job.kind == JobKind.CHECK_EXPECTATIONS

    after = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    now = datetime(2026, 10, 31, 10, 2, 30, tzinfo=UTC)

    dues = await job.due(project_id, after, now)
    # interval_dues returns the latest grid point in (after, now]
    assert len(dues) == 1
    assert dues[0].kind == JobKind.CHECK_EXPECTATIONS
    assert dues[0].due == datetime(2026, 10, 31, 10, 2, 0, tzinfo=UTC)

    await job.run(project_id, dues[0])
    assert len(fake_exp.evaluated) == 1
    assert fake_exp.evaluated[0] == (project_id, dues[0].due)
