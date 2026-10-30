"""The data_summary hook's time budget, timed-out tables and the event loop."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mlops_agents.agents.hooks import data_summary as hook_module
from mlops_agents.data import cache as cache_module
from mlops_agents.data import summaries as summaries_module
from mlops_agents.data.constants import HOOK_BUDGET_S
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import SummaryEnv, build_summary_env, csv_rows

pytestmark = pytest.mark.serial

BUDGET_S = 0.5
# Windows CI is slow to start threads and scan folders; the bound only has to be far below
# the full profile timeout to show the budget cut the render.
MARGIN_S = 3.0


@pytest.fixture
async def env(
    tmp_path: Path, paths: DataPaths, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> SummaryEnv:
    return await build_summary_env(tmp_path, paths, databases, monkeypatch)


def _short_full_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """The full profile timeout, as every module that reads it sees it."""
    for module in (hook_module, summaries_module, cache_module):
        monkeypatch.setattr(module, "PROFILE_TIMEOUT_S", seconds)


async def test_the_budget_bounds_a_cold_render_and_lists_the_tables_past_it(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = ("s1", "s2", "s3")
    await env.connect("slow", {f"{name}.csv": csv_rows(3) for name in tables})
    env.spy.slow.update(f"{name}.csv" for name in tables)
    monkeypatch.setattr(hook_module, "HOOK_BUDGET_S", BUDGET_S)

    started = time.monotonic()
    text = await env.render()

    assert time.monotonic() - started < BUDGET_S + MARGIN_S
    assert text.endswith(
        "\n".join(f'Table "{name}" (csv): detail left out: time budget' for name in tables)
    )
    assert (env.spy.count("s1.csv"), env.spy.count("s2.csv"), env.spy.count("s3.csv")) == (1, 0, 0)


async def test_a_budget_cut_table_is_profiled_again_with_a_full_budget(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.connect("sales", {"a.csv": csv_rows(3)})
    env.spy.slow_once.add("a.csv")
    monkeypatch.setattr(hook_module, "HOOK_BUDGET_S", BUDGET_S)
    assert (await env.render()).endswith('\nTable "a" (csv): detail left out: time budget')
    assert not list(env.paths.cache_dir(env.project_id).glob("*.json"))

    monkeypatch.setattr(hook_module, "HOOK_BUDGET_S", HOOK_BUDGET_S)
    text = await env.render()

    assert env.spy.count("a.csv") == 2
    assert 'Table "a" (csv, files ["a.csv"], data version' in text
    assert "): 3 rows" in text


async def test_a_table_timed_out_under_the_full_timeout_is_a_cache_hit_that_says_so(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.connect("sales", {"a.csv": csv_rows(3), "b.csv": csv_rows(3)})
    env.spy.slow.add("a.csv")
    _short_full_timeout(monkeypatch, 0.3)
    timed_out = "Profile timed out after 0.3 s: the statistics and sample rows are unknown."

    first = await env.render()
    second = await env.render()

    assert env.spy.count("a.csv") == 1
    assert second == first
    lines = first.split("\n")
    title = lines[lines.index(timed_out) - 1]
    assert title.startswith('Table "a" (csv, files ["a.csv"], ')
    assert title.endswith("): row count unknown (timed out)")
    assert any(line.startswith('Table "b" (csv, ') and line.endswith("3 rows") for line in lines)


async def test_a_render_waiting_on_another_renders_profile_keeps_to_its_budget(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache's lock does not know a waiter's budget: the waiter stops waiting itself."""
    await env.connect("sales", {"a.csv": csv_rows(3)})
    env.spy.slow.add("a.csv")
    _short_full_timeout(monkeypatch, 3)
    first = asyncio.create_task(env.render())
    await env.spy.started("a.csv")
    monkeypatch.setattr(hook_module, "HOOK_BUDGET_S", 0.3)

    started = time.monotonic()
    waiting = await env.render()

    assert time.monotonic() - started < 2.5
    assert waiting.endswith('\nTable "a" (csv): detail left out: time budget')
    assert not first.done()
    assert "Profile timed out after 3 s" in await first
    assert env.spy.count("a.csv") == 1


async def test_the_event_loop_keeps_running_during_a_cold_render(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.connect("sales", {"a.csv": csv_rows(3), "b.csv": csv_rows(3)})
    env.spy.slow.add("a.csv")
    _short_full_timeout(monkeypatch, 0.3)
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    text = await env.render()
    done.set()
    await task

    assert 'Table "b" (csv, ' in text
    assert ticks >= 10
