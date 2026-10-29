"""Table profiles through the cache, for the `data_summary` hook and the table-detail route."""

from __future__ import annotations

import asyncio
from typing import Final

from mlops_agents.connections.contract import ColumnInfo, Connections, TableDetail, TableRef
from mlops_agents.data.cache import ProfileCache
from mlops_agents.data.constants import PROFILE_TIMEOUT_S
from mlops_agents.data.profile import TableProfile, profile_table
from mlops_agents.data.render import render_table

# How long past its own `timeout_s` a caller waits. A profile stops itself at `timeout_s`
# (its Deadline interrupts DuckDB; cancelling the worker thread would not), so the wait only
# bounds a caller queued behind another caller's longer profile of the same table.
_WAIT_GRACE_S: Final = 1.0


class DataSummaries:
    def __init__(self, connections: Connections, cache: ProfileCache) -> None:
        self._connections = connections
        self._cache = cache

    async def table_detail(
        self, project_id: str, connection_id: str, table: TableRef
    ) -> TableDetail:
        """Raises `DomainError` as `Connections` and `profile_table` do, and `TimeoutError`
        as `versioned_profile` does."""
        data_version, profile = await self.versioned_profile(
            project_id, connection_id, table, PROFILE_TIMEOUT_S
        )
        return TableDetail(
            table=table,
            columns=[ColumnInfo(name=c.name, type=c.type) for c in profile.columns],
            row_count=profile.row_count,
            data_version=data_version,
            summary=render_table(table, profile),
        )

    async def profile(
        self,
        project_id: str,
        connection_id: str,
        table: TableRef,
        timeout_s: float = PROFILE_TIMEOUT_S,
    ) -> TableProfile:
        _, profile = await self.versioned_profile(project_id, connection_id, table, timeout_s)
        return profile

    async def versioned_profile(
        self, project_id: str, connection_id: str, table: TableRef, timeout_s: float
    ) -> tuple[str, TableProfile]:
        """The table's data version, read now, and its profile at that version.

        Raises `TimeoutError` when another caller's profile of the table, which this one
        waits for, outlasts `timeout_s`.
        """
        data_version = await self._connections.data_version(project_id, connection_id, table.name)
        source = await self._connections.duckdb_source(project_id, connection_id, table.name)

        async def compute() -> TableProfile:
            return await profile_table(source, timeout_s)

        profile = await asyncio.wait_for(
            self._cache.get_or_compute(
                project_id, connection_id, table.name, data_version, source.sql_from, compute
            ),
            timeout_s + _WAIT_GRACE_S,
        )
        return data_version, profile
