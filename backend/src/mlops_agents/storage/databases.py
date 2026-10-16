from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.paths import DataPaths
from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import create_engine_for


class Databases:
    """One migrated engine for app.db and one per project.db, created on first use."""

    def __init__(self, paths: DataPaths) -> None:
        self._paths = paths
        self._app: AsyncEngine | None = None
        self._projects: dict[str, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def app(self) -> AsyncEngine:
        async with self._lock:
            if self._app is None:
                await asyncio.to_thread(
                    migrate, self._paths.app_db, "app", self._paths.backups / "migrations" / "app"
                )
                self._app = create_engine_for(self._paths.app_db)
            return self._app

    async def project(self, project_id: str) -> AsyncEngine:
        async with self._lock:
            engine = self._projects.get(project_id)
            if engine is None:
                db_path = self._paths.project_db(project_id)
                backup_dir = self._paths.backups / "migrations" / "projects" / project_id
                await asyncio.to_thread(migrate, db_path, "project", backup_dir)
                engine = create_engine_for(db_path)
                self._projects[project_id] = engine
            return engine

    async def close_project(self, project_id: str) -> None:
        """Release the project's database files (needed before deleting them on Windows)."""
        async with self._lock:
            engine = self._projects.pop(project_id, None)
        if engine is not None:
            await engine.dispose()

    async def dispose(self) -> None:
        async with self._lock:
            engines = list(self._projects.values())
            if self._app is not None:
                engines.append(self._app)
            self._projects.clear()
            self._app = None
        for engine in engines:
            await engine.dispose()
