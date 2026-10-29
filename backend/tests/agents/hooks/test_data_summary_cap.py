"""The one size cap over the whole data_summary text (Decision #430): the reviewer's three
measured folders, at the real cap."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mlops_agents.agents.hooks import data_summary as hook_module
from mlops_agents.data import render
from mlops_agents.data.constants import DATA_SUMMARY_MAX_CHARS, TEXT_CUT
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import SummaryEnv, build_summary_env

UNREADABLE = re.compile(r'^Table "[^"]+" \(csv\): unreadable: ')
# The longest an unreadable line can be: its prefix and the cut reason, escaped.
UNREADABLE_LINE_MAX = 60 + 2 * TEXT_CUT


@pytest.fixture
async def env(
    tmp_path: Path, paths: DataPaths, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> SummaryEnv:
    return await build_summary_env(tmp_path, paths, databases, monkeypatch)


def _accounted(text: str) -> int:
    """Connections and tables the text names, lists or counts."""
    lines = text.split("\n")
    named = sum(1 for line in lines if line.startswith(("Connection ", "Table ")))
    listed = sum(int(n) for n in re.findall(r"^and (\d+) more tables: ", text, re.MULTILINE))
    counted = re.findall(r"^and (\d+) more entries left out: size cap$", text, re.MULTILINE)
    return named + listed + sum(int(n) for n in counted)


def _assert_no_host_path(text: str, tmp_path: Path) -> None:
    # The folder name survives any slash style or JSON escaping of a leaked path; the team
    # folders and the app data folder are all under it.
    assert tmp_path.name not in text


async def test_two_connections_of_latin1_exports(env: SummaryEnv, tmp_path: Path) -> None:
    """Excel's CSV export on Windows: DuckDB refuses each file as not UTF-8."""
    export = "id;nom;ville\n1;Renée;Orléans\n2;Zoë;Besançon\n".encode("latin-1")
    for name in ("exports", "archive"):
        await env.connect(name, {f"clients_{i:02}.csv": export for i in range(25)})

    text = await env.render()

    assert len(text) <= DATA_SUMMARY_MAX_CHARS
    tables = [line for line in text.split("\n") if line.startswith("Table ")]
    assert tables
    assert all(UNREADABLE.match(line) for line in tables)
    assert all(len(line) <= UNREADABLE_LINE_MAX for line in tables)
    # The cause stays (Decision #431); DuckDB's copy of the team's row does not.
    assert all(line.endswith("This file is not utf-8 encoded.") for line in tables)
    assert "id;nom;ville" not in text
    assert _accounted(text) == 2 + 2 * 25
    _assert_no_host_path(text, tmp_path)


async def test_a_connection_of_corrupt_files(env: SummaryEnv, tmp_path: Path) -> None:
    bad = b"a,b\n" + b"x" * 400 + b",\xff\xfe\n"
    await env.connect("broken", {f"bad_{i:02}.csv": bad for i in range(20)})

    text = await env.render()

    assert len(text) <= DATA_SUMMARY_MAX_CHARS
    tables = [line for line in text.split("\n") if line.startswith("Table ")]
    assert len(tables) == 20
    assert all(UNREADABLE.match(line) for line in tables)
    assert all(len(line) <= UNREADABLE_LINE_MAX for line in tables)
    assert _accounted(text) == 1 + 20
    _assert_no_host_path(text, tmp_path)


async def test_a_full_cap_then_many_long_named_tables(env: SummaryEnv, tmp_path: Path) -> None:
    """The first connection's detail fills the cap; four more connections follow."""
    wide_rows = "".join(
        ",".join(f"{column}-value-{row:04}-padding" for column in "abcd") + "\n"
        for row in range(25)
    )
    await env.connect("detail", {f"t{i:02}.csv": "a,b,c,d\n" + wide_rows for i in range(20)})
    for k in range(4):
        long_names: dict[str, str | bytes] = {
            f"quarterly_revenue_by_region_sales_channel_and_product_{i:03}_{k}.csv": "a\n1\n"
            for i in range(60)
        }
        await env.connect(f"long-{k}", long_names)

    text = await env.render()

    assert len(text) <= DATA_SUMMARY_MAX_CHARS
    assert re.search(r"\nand \d+ more entries left out: size cap$", text)
    assert _accounted(text) == (1 + 20) + 4 * (1 + 60)
    # Each table is tried until the stop (Decision #431), and none after it.
    named = set(re.findall(r'^Table "([^"]+)"', text, re.MULTILINE))
    assert {name.removesuffix(".csv") for name in env.spy.calls} == named
    _assert_no_host_path(text, tmp_path)


async def test_a_reason_naming_the_folder_names_its_sandbox_path(
    env: SummaryEnv, tmp_path: Path
) -> None:
    """Parquet and JSON errors name the file on their first line: the host folder must
    become the connection's /data path there (Decision #430)."""
    await env.connect("broken", {"bad.parquet": b"not a parquet file", "bad.json": b'{"a": 1,,}'})

    text = await env.render()

    lines = text.split("\n")
    assert any(line.startswith('Table "bad.json" (json): unreadable: ') for line in lines)
    assert any(line.startswith('Table "bad.parquet" (parquet): unreadable: ') for line in lines)
    assert "/data/broken/bad.parquet" in text
    assert "/data/broken/bad.json" in text
    _assert_no_host_path(text, tmp_path)


async def test_tables_after_a_stop_on_a_listed_line_are_never_profiled(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop can fall on an unreadable line while table detail still fits: the rest of
    the connection is counted, not profiled."""
    await env.connect(
        "sales", {"a.csv": b"a,b\n1,\xff\xfe\n", "b.csv": "x\n1\n", "c.csv": "x\n1\n"}
    )
    kept = f'{render.header()}\nConnection "sales" (local_folder, ok) at /data/sales:'
    # Room for a's listed line, so a is profiled, but not for its longer unreadable line.
    listed = '\nTable "a" (csv): detail left out: size cap'
    monkeypatch.setattr(
        hook_module,
        "DATA_SUMMARY_MAX_CHARS",
        len(kept) + len(listed) + hook_module.COUNTED_LINE_RESERVE,
    )

    text = await env.render()

    assert text == f"{kept}\nand 3 more entries left out: size cap"
    assert dict(env.spy.calls) == {"a.csv": 1}
