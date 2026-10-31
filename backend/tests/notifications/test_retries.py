from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from mlops_agents.monitoring.jobs import JobKind
from mlops_agents.notifications.retries import RetryJob
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases


class FixedClock(Clock):
    def __init__(self, now_dt: datetime) -> None:
        self._now = now_dt

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        pass


async def test_retry_job_due_and_run(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    clock = FixedClock(now)

    mock_notifier = AsyncMock()
    job = RetryJob(databases, mock_notifier, clock)

    assert job.kind == JobKind.RETRY_NOTIFICATIONS

    # Due based on 60s collection interval
    after = now - timedelta(seconds=120)
    dues = await job.due("prj_1", after, now)
    assert len(dues) == 1
    assert dues[0].kind == JobKind.RETRY_NOTIFICATIONS
    assert dues[0].due == now

    # Run delegates to notifier.retry_pending
    await job.run("prj_1", dues[0])
    mock_notifier.retry_pending.assert_awaited_once_with("prj_1", now)
