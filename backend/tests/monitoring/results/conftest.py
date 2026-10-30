"""Shared fixtures for results tests."""

from __future__ import annotations

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
    ResultsObserver,
    StoredResult,
)
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
