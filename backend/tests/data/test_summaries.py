"""DataSummaries: a table's detail and profile through the cache."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mlops_agents.connections.contract import ColumnInfo, TableRef
from mlops_agents.data.render import render_table
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import SummaryEnv, build_summary_env, csv_rows


@pytest.fixture
async def env(
    tmp_path: Path, paths: DataPaths, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> SummaryEnv:
    return await build_summary_env(tmp_path, paths, databases, monkeypatch)


async def _people(env: SummaryEnv) -> tuple[str, TableRef]:
    await env.connect("sales", {"people.csv": csv_rows(3)})
    (view,) = await env.connections.list(env.project_id)
    (ref,) = await env.connections.tables(env.project_id, view.id)
    return view.id, ref


async def test_table_detail_comes_from_the_profile(env: SummaryEnv) -> None:
    con, ref = await _people(env)

    detail = await env.summaries.table_detail(env.project_id, con, ref)

    profile = await env.summaries.profile(env.project_id, con, ref)
    assert detail.table == ref
    assert detail.columns == [
        ColumnInfo(name="id", type="BIGINT"),
        ColumnInfo(name="name", type="VARCHAR"),
    ]
    assert detail.row_count == 3
    assert detail.data_version == await env.connections.data_version(env.project_id, con, "people")
    assert detail.summary == render_table(ref, profile)
    assert env.spy.count("people.csv") == 1


async def test_a_second_profile_is_a_cache_hit(env: SummaryEnv) -> None:
    con, ref = await _people(env)
    first = await env.summaries.profile(env.project_id, con, ref)
    second = await env.summaries.profile(env.project_id, con, ref)
    assert first == second
    assert env.spy.count("people.csv") == 1


async def test_waiting_on_another_callers_profile_is_bounded_by_its_own_timeout(
    env: SummaryEnv,
) -> None:
    """The cache's lock is not bounded by the waiter's timeout: the waiter bounds itself."""
    con, ref = await _people(env)
    env.spy.slow.add("people.csv")
    first = asyncio.create_task(env.summaries.profile(env.project_id, con, ref, timeout_s=3))
    await env.spy.started("people.csv")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await env.summaries.profile(env.project_id, con, ref, timeout_s=0.2)
    assert time.monotonic() - started < 2.5
    assert not first.done()
    assert (await first).timed_out
    assert env.spy.count("people.csv") == 1
