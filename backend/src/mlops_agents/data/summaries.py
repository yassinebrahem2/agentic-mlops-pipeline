"""Table profiles through the cache, for the `data_summary` hook and the table-detail route."""

from __future__ import annotations

import asyncio
from typing import Final

from mlops_agents.agents.tools.files import is_secret
from mlops_agents.connections.contract import (
    ColumnInfo,
    Connections,
    DuckDbSource,
    TableDetail,
    TableRef,
)
from mlops_agents.data.cache import ProfileCache
from mlops_agents.data.constants import MAX_CONCURRENT_PROFILES, PROFILE_TIMEOUT_S
from mlops_agents.data.profile import TableProfile, TableUnreadable, profile_table
from mlops_agents.data.render import DENYLISTED_FILE, render_table
from mlops_agents.errors import DomainError

# How long past its own `timeout_s` a caller waits. A profile stops itself at `timeout_s`
# (its Deadline interrupts DuckDB; cancelling the worker thread would not), so the wait only
# bounds a caller queued behind another caller's longer profile of the same table, or behind
# the profiles holding every slot: a caller whose budget is spent while queued is cut.
_WAIT_GRACE_S: Final = 1.0

PROFILE_TIMEOUT_CODE: Final = "profile_timeout"
# Fixed: nothing from the request or the team's files goes into it.
PROFILE_TIMEOUT_MESSAGE: Final = "The table's profile did not finish within the time limit."


def _profile_timeout() -> DomainError:
    return DomainError(PROFILE_TIMEOUT_CODE, PROFILE_TIMEOUT_MESSAGE, status=504)


class SecretTable(TableUnreadable):
    """A file of the table matches the secret denylist the connection's scan reports
    (Decision #431): it is never profiled or sampled, so its contents never reach a prompt,
    a checkpoint or the cache."""

    def __init__(self) -> None:
        super().__init__(f"This table {DENYLISTED_FILE}, so it is not profiled.", folders=[])


class DataSummaries:
    def __init__(self, connections: Connections, cache: ProfileCache) -> None:
        self._connections = connections
        self._cache = cache
        # Each profile holds a DuckDB of up to DUCKDB_MEMORY_LIMIT (Decision #432).
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_PROFILES)
        self._running: set[asyncio.Task[TableProfile]] = set()

    async def table_detail(
        self, project_id: str, connection_id: str, table: TableRef
    ) -> TableDetail:
        """A timed-out profile with columns is a detail (`timed_out`; `row_count` None only
        when the count did not finish), and so is an incomplete one (`incomplete`).

        Raises `DomainError` as `Connections` and `profile_table` do, and
        `DomainError("profile_timeout")` (504) when nothing was computed or waiting on
        another caller's profile of the table timed out.
        """
        try:
            data_version, profile = await self.versioned_profile(
                project_id, connection_id, table, PROFILE_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise _profile_timeout() from exc
        if profile.timed_out and not profile.columns:
            raise _profile_timeout()
        return TableDetail(
            table=table,
            columns=[ColumnInfo(name=c.name, type=c.type) for c in profile.columns],
            row_count=profile.row_count,
            timed_out=profile.timed_out,
            incomplete=profile.incomplete,
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
        """Raises `DomainError` as `table_detail` does, except that a timed-out profile is
        returned as it is, with or without columns."""
        try:
            _, profile = await self.versioned_profile(project_id, connection_id, table, timeout_s)
        except TimeoutError as exc:
            raise _profile_timeout() from exc
        return profile

    async def versioned_profile(
        self, project_id: str, connection_id: str, table: TableRef, timeout_s: float
    ) -> tuple[str, TableProfile]:
        """The table's data version, read now, and its profile at that version.

        Raises `SecretTable` before anything is read, and `TimeoutError` when the waits for
        another caller's profile of the table or for a free profile slot, together with the
        profile, outlast `timeout_s`.
        """
        if any(is_secret(file) for file in table.files):
            raise SecretTable
        data_version = await self._connections.data_version(project_id, connection_id, table.name)
        source = await self._connections.duckdb_source(project_id, connection_id, table.name)

        async def compute() -> TableProfile:
            return await self._profile_in_a_slot(source, timeout_s)

        profile = await asyncio.wait_for(
            self._cache.get_or_compute(
                project_id, connection_id, table.name, data_version, source.sql_from, compute
            ),
            timeout_s + _WAIT_GRACE_S,
        )
        return data_version, profile

    async def _profile_in_a_slot(self, source: DuckDbSource, timeout_s: float) -> TableProfile:
        """`profile_table` once one of the `MAX_CONCURRENT_PROFILES` slots is free; its
        `timeout_s` starts then. The slot is held until the profile's thread ends, also when
        the caller stops waiting: the thread cannot be cancelled and runs to its deadline."""
        await self._slots.acquire()
        running = asyncio.create_task(profile_table(source, timeout_s))
        self._running.add(running)
        running.add_done_callback(self._free_slot)
        return await asyncio.shield(running)

    def _free_slot(self, running: asyncio.Task[TableProfile]) -> None:
        self._running.discard(running)
        self._slots.release()
        if not running.cancelled():
            running.exception()  # retrieved: a caller that stopped waiting no longer will
