"""At most `MAX_CONCURRENT_PROFILES` profiles at once, each holding a DuckDB of up to
`DUCKDB_MEMORY_LIMIT`, and the callers waiting for a slot (Decision #432)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest

from mlops_agents.agents.hooks import data_summary as hook_module
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, TableRef
from mlops_agents.data import profile as profile_module
from mlops_agents.data.constants import MAX_CONCURRENT_PROFILES
from mlops_agents.data.duck import locked_connection
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import SummaryEnv, build_summary_env, csv_rows, write

OTHER_PROJECT = "prj_other"
# Windows CI is slow to start threads; the bounds only have to be far below the waits that a
# missing cut would cause.
MARGIN_S = 3.0


class Gauge:
    """The profiles' DuckDB connections: how many are open at once. Each opening waits for
    `gate`, so the test decides when the profiles holding the slots end."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.open = 0
        self.peak = 0
        self._changed = threading.Condition()

    def connect(self, allowed: Sequence[Path]) -> duckdb.DuckDBPyConnection:
        with self._changed:
            self.open += 1
            self.peak = max(self.peak, self.open)
            self._changed.notify_all()
        self.gate.wait(timeout=30)
        return cast(duckdb.DuckDBPyConnection, _Counted(locked_connection(allowed), self))

    def closed(self) -> None:
        with self._changed:
            self.open -= 1
            self._changed.notify_all()

    async def until_open(self, count: int) -> None:
        def wait() -> bool:
            with self._changed:
                return self._changed.wait_for(lambda: self.open >= count, timeout=10)

        assert await asyncio.to_thread(wait)


class _Counted:
    """A connection that tells the gauge when it is closed."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, gauge: Gauge) -> None:
        self._connection = connection
        self._gauge = gauge

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def close(self) -> None:
        self._connection.close()
        self._gauge.closed()


@pytest.fixture
async def env(
    tmp_path: Path, paths: DataPaths, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> SummaryEnv:
    return await build_summary_env(tmp_path, paths, databases, monkeypatch)


@pytest.fixture
async def gauge(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Gauge]:
    counted = Gauge()
    monkeypatch.setattr(profile_module, "locked_connection", counted.connect)
    yield counted
    counted.gate.set()  # no profile thread outlives the test


async def _tables(env: SummaryEnv, names: str) -> tuple[str, list[TableRef]]:
    await env.connect("sales", {f"{name}.csv": csv_rows(3) for name in names})
    (view,) = await env.connections.list(env.project_id)
    return view.id, await env.connections.tables(env.project_id, view.id)


def _assert_profile_timeout(error: DomainError) -> None:
    assert (error.code, error.status) == ("profile_timeout", 504)


async def test_at_most_two_profiles_run_at_once(env: SummaryEnv, gauge: Gauge) -> None:
    con, refs = await _tables(env, "abcde")
    tasks = [asyncio.create_task(env.summaries.profile(env.project_id, con, r)) for r in refs]
    await gauge.until_open(MAX_CONCURRENT_PROFILES)
    await asyncio.sleep(0.3)
    assert env.spy.total == MAX_CONCURRENT_PROFILES == 2

    gauge.gate.set()
    profiles = await asyncio.gather(*tasks)

    assert [p.row_count for p in profiles] == [3] * 5
    assert gauge.peak == MAX_CONCURRENT_PROFILES
    assert env.spy.total == 5


async def test_a_caller_waiting_for_a_slot_is_cut_by_its_own_timeout(
    env: SummaryEnv, gauge: Gauge
) -> None:
    con, (a, b, c) = await _tables(env, "abc")
    holders = [
        asyncio.create_task(env.summaries.profile(env.project_id, con, ref)) for ref in (a, b)
    ]
    await gauge.until_open(2)

    started = time.monotonic()
    with pytest.raises(DomainError) as caught:
        await env.summaries.profile(env.project_id, con, c, timeout_s=0.2)

    assert time.monotonic() - started < MARGIN_S
    _assert_profile_timeout(caught.value)
    assert env.spy.count("c.csv") == 0
    gauge.gate.set()
    await asyncio.gather(*holders)


async def test_the_wait_for_a_slot_does_not_shorten_the_profiles_own_timeout(
    env: SummaryEnv, gauge: Gauge
) -> None:
    con, (a, b, c) = await _tables(env, "abc")
    holders = [
        asyncio.create_task(env.summaries.profile(env.project_id, con, ref)) for ref in (a, b)
    ]
    await gauge.until_open(2)
    waiting = asyncio.create_task(env.summaries.profile(env.project_id, con, c, timeout_s=2.0))
    await asyncio.sleep(0.5)
    assert env.spy.count("c.csv") == 0

    gauge.gate.set()
    profile = await waiting

    assert profile.timeout_s == 2.0
    assert not profile.timed_out
    assert profile.row_count == 3
    await asyncio.gather(*holders)


async def test_a_slot_is_held_until_the_profile_of_a_cut_caller_ends(
    env: SummaryEnv, gauge: Gauge
) -> None:
    """The profile's thread cannot be cancelled: its slot is free only when it ends."""
    con, (a, b, c) = await _tables(env, "abc")
    cut = asyncio.create_task(env.summaries.profile(env.project_id, con, a, timeout_s=0.2))
    held = asyncio.create_task(env.summaries.profile(env.project_id, con, b))
    await gauge.until_open(2)
    with pytest.raises(DomainError) as caught:
        await cut
    _assert_profile_timeout(caught.value)

    later = asyncio.create_task(env.summaries.profile(env.project_id, con, c))
    await asyncio.sleep(0.3)
    assert env.spy.count("c.csv") == 0
    assert gauge.open == 2

    gauge.gate.set()
    assert (await later).row_count == 3
    assert (await held).row_count == 3
    assert gauge.peak == 2


async def test_a_render_waiting_for_a_slot_keeps_to_its_budget(
    env: SummaryEnv,
    gauge: Gauge,
    databases: Databases,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another project's profiles hold both slots: the hook's budget cuts the wait."""
    await databases.project(OTHER_PROJECT)
    busy = tmp_path / "team busy"
    for name in ("a", "b"):
        write(busy / f"{name}.csv", csv_rows(3))
    other = await env.connections.create(
        OTHER_PROJECT, LOCAL_FOLDER_KIND, "busy", {"path": str(busy)}
    )
    holders = [
        asyncio.create_task(env.summaries.profile(OTHER_PROJECT, other.id, ref))
        for ref in await env.connections.tables(OTHER_PROJECT, other.id)
    ]
    await gauge.until_open(2)
    await env.connect("sales", {"c.csv": csv_rows(3)})
    monkeypatch.setattr(hook_module, "HOOK_BUDGET_S", 0.5)

    started = time.monotonic()
    text = await env.render()

    assert time.monotonic() - started < 0.5 + MARGIN_S
    assert text.endswith('\nTable "c" (csv): detail left out: time budget')
    assert env.spy.count("c.csv") == 0
    gauge.gate.set()
    await asyncio.gather(*holders)
