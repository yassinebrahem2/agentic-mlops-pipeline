"""Fixtures for tracking service and routes tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert

from mlops_agents.clock import now_iso
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.tracking.contract import (
    SourceType,
    SyncResult,
    TrackingSourceView,
)
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore


class FakeTrackingAdapter:
    def __init__(
        self,
        source_type: SourceType = "mlflow",
        result: SyncResult | None = None,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.type: SourceType = source_type
        self.result: SyncResult = result or SyncResult(
            versions=[],
            runs=[],
            metrics=[],
            aliases={},
            ci_runs=[],
            evaluations=[],
            error=None,
        )
        self.error: Exception | None = error
        self.delay_s: float = delay_s
        self.calls: list[dict[str, Any]] = []

    async def sync(
        self, source: TrackingSourceView, credential: str | None, since: datetime | None
    ) -> SyncResult:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        self.calls.append({"source": source, "credential": credential, "since": since})
        if self.error is not None:
            raise self.error
        return self.result


class TestClock:
    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = datetime.fromtimestamp(self._now.timestamp() + seconds, tz=UTC)

    def set(self, dt: datetime) -> None:
        self._now = dt


@dataclass
class TrackingEnv:
    service: TrackingService
    databases: Databases
    keystore: MemoryKeyStore
    secrets: ProjectSecrets
    events: SqliteEventLog
    adapters: dict[SourceType, FakeTrackingAdapter]
    project_id: str
    clock: TestClock
    github_token_value: str | None = "fake-gh-token"

    async def get_github_token(self) -> str | None:
        return self.github_token_value


@pytest.fixture
def test_clock() -> TestClock:
    return TestClock()


@pytest.fixture
async def tracking_env(
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    test_clock: TestClock,
) -> AsyncIterator[TrackingEnv]:
    project_id = "prj_test"
    # Create the project row in app.db so it's recognized as existing
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="Test Project",
                source="github",
                github_full_name="org/demo",
                github_key="org/demo",
                default_branch="main",
                status="ok",
                created_at=now_iso(),
            )
        )
    # Ensure project database is initialized
    await databases.project(project_id)

    secrets = ProjectSecrets(keystore, databases)
    events = SqliteEventLog(databases)
    mlflow_adapter = FakeTrackingAdapter("mlflow")
    adapters: dict[SourceType, FakeTrackingAdapter] = {"mlflow": mlflow_adapter}

    env = TrackingEnv(
        service=None,  # type: ignore[arg-type]
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        project_id=project_id,
        clock=test_clock,
        github_token_value="fake-gh-token",
    )

    async def gh_token() -> str | None:
        return env.github_token_value

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,  # type: ignore[arg-type]
        github_token=gh_token,
        clock=test_clock,
    )
    env.service = service

    yield env
    await service.close()
