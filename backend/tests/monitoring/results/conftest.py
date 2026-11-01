"""Shared fixtures for results tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.connections.contract import (
    LOCAL_FOLDER_KIND,
    ConnectionView,
    DuckDbSource,
    Finding,
    Findings,
    Mount,
    TableRef,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.contract import (
    ResultRecord,
    ResultsObserver,
    SourceState,
    StoredResult,
)
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.proposals.contract import HandoffObserver, HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock


class FakeObserver(ResultsObserver):
    def __init__(self, name: str = "fake", should_raise: Exception | None = None) -> None:
        self.name = name
        self.should_raise = should_raise
        self.batches: list[list[StoredResult]] = []

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.batches.append(list(results))


class HoldingObserver(ResultsObserver):
    """Records every batch; holds its first call for `hold_project` until `release` is set."""

    def __init__(self, hold_project: str) -> None:
        self._hold_project = hold_project
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.batches: list[tuple[str, list[str]]] = []

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        self.batches.append((project_id, [r.id for r in results]))
        if project_id == self._hold_project and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()


class InMemoryResultsStore(ResultsStore):
    """Results rows and source states in memory. No method awaits, so the only suspension
    points in a delivery are the service's own and the observer's."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self.undelivered: dict[str, list[StoredResult]] = {}
        self.mark_calls: list[tuple[str, list[str]]] = []
        self.states: dict[tuple[str, str], SourceState] = {}
        self.attempts: dict[str, int] = {}

    async def fetch_undelivered(self, project_id: str, limit: int = 500) -> list[StoredResult]:
        return list(self.undelivered.get(project_id, []))[:limit]

    async def increment_delivery_attempts(
        self, project_id: str, ids: Sequence[str]
    ) -> dict[str, int]:
        res: dict[str, int] = {}
        for r_id in ids:
            cnt = self.attempts.get(r_id, 0) + 1
            self.attempts[r_id] = cnt
            res[r_id] = cnt
        return res

    async def get_delivery_attempts(self, project_id: str, ids: Sequence[str]) -> dict[str, int]:
        return {r_id: self.attempts.get(r_id, 0) for r_id in ids}

    async def mark_delivered(
        self, project_id: str, ids: Sequence[str], delivered_at: datetime
    ) -> None:
        if not ids:
            return
        self.mark_calls.append((project_id, list(ids)))
        marked = set(ids)
        rows = self.undelivered.get(project_id, [])
        self.undelivered[project_id] = [r for r in rows if r.id not in marked]

    async def get_source_state(self, project_id: str, source_id: str) -> SourceState | None:
        return self.states.get((project_id, source_id))

    async def save_source_state(self, project_id: str, state: SourceState) -> None:
        self.states[(project_id, state.source_id)] = state

    async def save_items_and_state(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
        state: SourceState,
    ) -> list[StoredResult]:
        inserted = [
            StoredResult(
                **record.model_dump(),
                id=f"res_{source_key}",
                source_id=source_id,
                source_key=source_key,
                seen_at=seen_at,
            )
            for source_key, record in items
        ]
        self.undelivered.setdefault(project_id, []).extend(inserted)
        self.states[(project_id, source_id)] = state
        return inserted


class FakeHandoffs:
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self._current: HandoffView | None = handoff

    def set_handoff(self, handoff: HandoffView | None) -> None:
        self._current = handoff

    def add_observer(self, observer: HandoffObserver) -> None:
        pass

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        raise NotImplementedError


class FakeConnections:
    def __init__(self, connections: list[ConnectionView] | None = None) -> None:
        self._connections: dict[str, ConnectionView] = {c.name: c for c in (connections or [])}

    def add_local_folder(
        self, name: str, folder: Path, secret_files: list[str] | None = None
    ) -> ConnectionView:
        findings = Findings(
            unsupported_files=[],
            secret_files=[Finding(path=s, reason="denylist") for s in (secret_files or [])],
            truncated=False,
        )
        view = ConnectionView(
            id=f"conn_{name}",
            name=name,
            kind=LOCAL_FOLDER_KIND,
            settings={"path": str(folder)},
            status="ok",
            status_detail=None,
            findings=findings,
            data_version="1",
        )
        self._connections[name] = view
        return view

    async def list(self, project_id: str) -> list[ConnectionView]:
        return list(self._connections.values())

    async def inspect(self, project_id: str, kind: str, settings: dict[str, Any]) -> Findings:
        # Match connection by settings path or return inspect findings
        for view in self._connections.values():
            if view.kind == kind and view.settings == settings:
                return view.findings
        return Findings(unsupported_files=[], secret_files=[], truncated=False)

    async def create(
        self, project_id: str, kind: str, name: str, settings: dict[str, Any]
    ) -> ConnectionView:
        raise NotImplementedError

    async def update(
        self, project_id: str, connection_id: str, settings: dict[str, Any]
    ) -> ConnectionView:
        raise NotImplementedError

    async def delete(self, project_id: str, connection_id: str) -> None:
        raise NotImplementedError

    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]:
        raise NotImplementedError

    async def data_version(self, project_id: str, connection_id: str, table: str) -> str:
        raise NotImplementedError

    async def duckdb_source(self, project_id: str, connection_id: str, table: str) -> DuckDbSource:
        raise NotImplementedError

    async def mounts(self, project_id: str) -> list[Mount]:
        raise NotImplementedError


@pytest.fixture
def project_id() -> str:
    return "prj_testresults"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def fake_observer() -> FakeObserver:
    return FakeObserver()
