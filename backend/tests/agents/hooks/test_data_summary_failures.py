"""The data_summary hook's one-line failures: unreachable connections, unreadable tables and
secret-named files, none naming a host folder (Decisions #430, #431)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.data.summary_env import SummaryEnv, build_summary_env, csv_rows, out_of_memory_at


@pytest.fixture
async def env(
    tmp_path: Path, paths: DataPaths, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> SummaryEnv:
    return await build_summary_env(tmp_path, paths, databases, monkeypatch)


async def test_an_unreachable_connection_gives_one_line(env: SummaryEnv, tmp_path: Path) -> None:
    folder = await env.connect("gone", {"a.csv": csv_rows(2)})
    await env.connect("kept", {"b.csv": csv_rows(2)})
    folder.rename(folder.with_name("moved away"))

    text = await env.render()

    lines = text.split("\n")
    (gone,) = [line for line in lines if '"gone"' in line]
    # The typed folder becomes the connection's sandbox path (Decision #431).
    assert gone == (
        'Connection "gone" (local_folder, unreachable): Folder does not exist: /data/gone'
    )
    assert tmp_path.name not in text
    assert 'Connection "kept" (local_folder, ok) at /data/kept:' in lines
    assert any(line.startswith('Table "b" (csv, ') for line in lines)


async def test_a_folder_gone_after_the_listing_names_no_host_path(
    env: SummaryEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kind's messages quote the folder as typed, in the race windows too."""
    folder = await env.connect("sales", {"a.csv": csv_rows(2)})
    gone = DomainError("connection_invalid", f"Folder does not exist: {folder}\nsecond line")

    async def vanished(*args: object) -> str:
        raise gone

    monkeypatch.setattr(env.connections, "data_version", vanished)
    profiled = await env.render()
    monkeypatch.setattr(env.connections, "tables", vanished)
    listed = await env.render()

    assert 'Table "a" (csv): unreadable: Folder does not exist: /data/sales' in profiled
    assert (
        'Connection "sales" (local_folder, unreachable): Folder does not exist: /data/sales'
    ) in listed
    assert tmp_path.name not in profiled + listed
    assert "second line" not in profiled + listed


async def test_a_corrupt_csv_gives_one_line_and_the_others_render(env: SummaryEnv) -> None:
    await env.connect("sales", {"bad.csv": b"a,b\n1,\xff\xfe\n", "good.csv": csv_rows(2)})

    lines = (await env.render()).split("\n")

    (bad,) = [line for line in lines if '"bad"' in line]
    assert bad.startswith('Table "bad" (csv): unreadable: ')
    assert any(line.startswith('Table "good" (csv, ') for line in lines)


async def test_an_out_of_memory_table_shows_what_finished_and_is_not_profiled_again(
    env: SummaryEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision #432: the statistics are kept, the marker says the rest is unknown, and the
    next turn reads the cached profile instead of running out of memory again."""
    await env.connect("sales", {"a.csv": csv_rows(3)})
    out_of_memory_at(monkeypatch)

    first = await env.render()
    second = await env.render()

    assert second == first
    assert env.spy.count("a.csv") == 1
    lines = first.split("\n")
    marker = "Profile incomplete (memory limit): anything not shown below is unknown."
    title = lines[lines.index(marker) - 1]
    assert title.startswith('Table "a" (csv, files ["a.csv"], data version ')
    assert title.endswith("): 3 rows")
    assert '- "id" BIGINT, nulls 0, ~distinct 3, 1..3' in lines
    assert "Sample rows: unknown" in lines
    assert "Out of Memory" not in first


@pytest.mark.parametrize("secret", ["credentials.json", "keys/client_secret.json"])
async def test_a_secret_named_file_is_listed_and_never_profiled(
    env: SummaryEnv, secret: str
) -> None:
    """The connection kind's secret denylist (Decision #431): its contents stay out of every
    agent's context and checkpoint."""
    token = "ghp_" + "t0ken" * 8
    await env.connect("sales", {secret: json.dumps({"token": token}), "people.csv": csv_rows(2)})
    name = secret.split("/")[0].removesuffix(".json")

    lines = (await env.render()).split("\n")

    assert f'Table "{name}" (json): detail left out: looks like a secret file' in lines
    assert not any(token in line for line in lines)
    assert env.spy.count(secret) == 0
    assert any(line.startswith('Table "people" (csv, ') for line in lines)
