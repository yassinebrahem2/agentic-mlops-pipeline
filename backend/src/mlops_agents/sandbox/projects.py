"""Project repository locations and persistent sandbox network settings."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.workspace.service import WorkspaceService


@runtime_checkable
class Mount(Protocol):
    """Read-only container bind mount."""

    @property
    def host_path(self) -> Path: ...

    @property
    def container_path(self) -> str: ...


class _Connections(Protocol):
    async def mounts(self, project_id: str) -> Sequence[Mount]: ...


class SandboxProjects(Protocol):
    async def root(self, project_id: str) -> Path: ...
    async def get_network(self, project_id: str) -> bool: ...
    async def set_network(self, project_id: str, enabled: bool) -> None: ...
    async def mounts(self, project_id: str) -> Sequence[Mount]: ...


class DatabaseSandboxProjects:
    def __init__(
        self,
        databases: Databases,
        workspace: WorkspaceService,
        connections: _Connections,
    ) -> None:
        self._databases = databases
        self._workspace = workspace
        self._connections = connections

    async def root(self, project_id: str) -> Path:
        project = await self._workspace.get(project_id)
        return self._workspace.repo_root(project)

    async def get_network(self, project_id: str) -> bool:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            value = (
                await conn.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == "sandbox_network"
                    )
                )
            ).scalar_one_or_none()
        return value is not None and value.strip().lower() == "true"

    async def set_network(self, project_id: str, enabled: bool) -> None:
        engine = await self._databases.project(project_id)
        value = "true" if enabled else "false"
        statement = insert(project_settings).values(key="sandbox_network", value=value)
        statement = statement.on_conflict_do_update(index_elements=["key"], set_={"value": value})
        async with engine.begin() as conn:
            await conn.execute(statement)

    async def mounts(self, project_id: str) -> Sequence[Mount]:
        return await self._connections.mounts(project_id)
