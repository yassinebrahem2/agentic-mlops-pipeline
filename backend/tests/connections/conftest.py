"""The connections service on a fresh data folder, shared by the service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases

ServiceEnv = tuple[ConnectionsService, Databases, SqliteEventLog, DataPaths]


@pytest.fixture
async def service_env(tmp_path: Path) -> AsyncIterator[ServiceEnv]:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    service = ConnectionsService(databases, events, registry)

    yield service, databases, events, paths
    await databases.dispose()
