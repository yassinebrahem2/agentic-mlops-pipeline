"""DataSummaries: a table's detail and profile through the cache, and their timeouts."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mlops_agents.connections.contract import ColumnInfo, DuckDbSource, TableRef
from mlops_agents.data import cache as cache_module
from mlops_agents.data import summaries as summaries_module
from mlops_agents.data.profile import TableProfile
from mlops_agents.data.render import render_table
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import (
    SummaryEnv,
    build_summary_env,
    csv_rows,
    out_of_memory_at,
    time_out_after_the_count,
)


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


def _short_full_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """The full profile timeout, as `DataSummaries` and the cache see it."""
    for module in (summaries_module, cache_module):
        monkeypatch.setattr(module, "PROFILE_TIMEOUT_S", seconds)


def _assert_profile_timeout(error: DomainError, ref: TableRef) -> None:
    assert (error.code, error.status) == ("profile_timeout", 504)
    assert error.message == summaries_module.PROFILE_TIMEOUT_MESSAGE
    assert ref.name not in error.message


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
    assert detail.timed_out is False
    assert detail.incomplete is False
    assert detail.data_version == await env.connections.data_version(env.project_id, con, "people")
    assert detail.summary == render_table(ref, profile)
    assert env.spy.count("people.csv") == 1


async def test_a_second_profile_is_a_cache_hit(env: SummaryEnv) -> None:
    con, ref = await _people(env)
    first = await env.summaries.profile(env.project_id, con, ref)
    second = await env.summaries.profile(env.project_id, con, ref)
    assert first == second
    assert env.spy.count("people.csv") == 1


async def test_a_timed_out_profile_with_columns_is_a_detail_with_an_unknown_row_count(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    con, ref = await _people(env)
    env.spy.slow.add("people.csv")
    _short_full_timeout(monkeypatch, 0.3)

    detail = await env.summaries.table_detail(env.project_id, con, ref)

    assert detail.timed_out is True
    assert detail.row_count is None
    assert detail.columns == [ColumnInfo(name="range", type="BIGINT")]
    assert "row count unknown (timed out)" in detail.summary


async def test_a_timed_out_profile_keeps_a_count_that_finished(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    con, ref = await _people(env)
    time_out_after_the_count(monkeypatch)

    detail = await env.summaries.table_detail(env.project_id, con, ref)

    assert detail.timed_out is True
    assert detail.row_count == 3
    assert "row count unknown" not in detail.summary
    assert detail.summary.split("\n", 1)[0].endswith("): 3 rows")


async def test_an_out_of_memory_profile_is_an_incomplete_detail_and_is_cached(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision #432: what finished is kept and not recomputed at every call."""
    con, ref = await _people(env)
    out_of_memory_at(monkeypatch)

    detail = await env.summaries.table_detail(env.project_id, con, ref)
    again = await env.summaries.table_detail(env.project_id, con, ref)

    assert detail.incomplete is True
    assert detail.timed_out is False
    assert detail.row_count == 3
    assert "Profile incomplete (memory limit)" in detail.summary
    assert again == detail
    assert env.spy.count("people.csv") == 1


async def test_a_timed_out_profile_without_columns_is_a_profile_timeout(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was computed: there is no detail to answer with."""
    con, ref = await _people(env)

    async def nothing_computed(source: DuckDbSource, timeout_s: float) -> TableProfile:
        return TableProfile(
            row_count=None,
            columns=[],
            more_columns=0,
            sample_columns=[],
            sample=[],
            timed_out=True,
            incomplete=False,
            timeout_s=timeout_s,
        )

    monkeypatch.setattr(summaries_module, "profile_table", nothing_computed)

    with pytest.raises(DomainError) as caught:
        await env.summaries.table_detail(env.project_id, con, ref)
    _assert_profile_timeout(caught.value, ref)


@pytest.mark.serial
async def test_waiting_on_another_callers_profile_is_bounded_by_its_own_timeout(
    env: SummaryEnv,
) -> None:
    """The cache's lock is not bounded by the waiter's timeout: the waiter bounds itself."""
    con, ref = await _people(env)
    env.spy.slow.add("people.csv")
    first = asyncio.create_task(env.summaries.profile(env.project_id, con, ref, timeout_s=3))
    await env.spy.started("people.csv")

    started = time.monotonic()
    with pytest.raises(DomainError) as caught:
        await env.summaries.profile(env.project_id, con, ref, timeout_s=0.2)
    assert time.monotonic() - started < 2.5
    _assert_profile_timeout(caught.value, ref)
    assert not first.done()
    assert (await first).timed_out
    assert env.spy.count("people.csv") == 1


@pytest.mark.serial
async def test_a_detail_waiting_on_another_callers_profile_is_a_profile_timeout(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    con, ref = await _people(env)
    env.spy.slow.add("people.csv")
    first = asyncio.create_task(env.summaries.profile(env.project_id, con, ref, timeout_s=3))
    await env.spy.started("people.csv")
    _short_full_timeout(monkeypatch, 0.2)

    started = time.monotonic()
    with pytest.raises(DomainError) as caught:
        await env.summaries.table_detail(env.project_id, con, ref)
    assert time.monotonic() - started < 2.5
    _assert_profile_timeout(caught.value, ref)
    assert (await first).timed_out
    assert env.spy.count("people.csv") == 1
