"""The data summaries over the real connections service and local folder kind, with a spy on
`profile_table` (folder-to-table discovery stays in its one place)."""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.data_summary import DataSummaryHook
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, DuckDbSource
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.data import profile as profile_module
from mlops_agents.data import summaries as summaries_module
from mlops_agents.data.cache import ProfileCache
from mlops_agents.data.duck import Deadline, DeadlineExpired
from mlops_agents.data.profile import TableProfile, profile_table
from mlops_agents.data.summaries import DataSummaries
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases

ENDLESS = "range(10000000000000)"


@dataclass
class ProfileSpy:
    """Counts `profile_table` calls per file name; a slow file's source never ends."""

    calls: Counter[str] = field(default_factory=Counter[str])
    slow: set[str] = field(default_factory=set[str])
    slow_once: set[str] = field(default_factory=set[str])
    _started: defaultdict[str, asyncio.Event] = field(
        default_factory=lambda: defaultdict(asyncio.Event)
    )

    def count(self, file_name: str) -> int:
        return self.calls[file_name]

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    async def started(self, file_name: str) -> None:
        """Returns once a profile of `file_name` has started."""
        await asyncio.wait_for(self._started[file_name].wait(), timeout=10)

    async def __call__(self, source: DuckDbSource, timeout_s: float) -> TableProfile:
        file_name = _first_file(source)
        self.calls[file_name] += 1
        self._started[file_name].set()
        if file_name in self.slow or file_name in self.slow_once:
            self.slow_once.discard(file_name)
            source = DuckDbSource(sql_from=ENDLESS, allowed_directories=source.allowed_directories)
        return await profile_table(source, timeout_s)


def _first_file(source: DuckDbSource) -> str:
    """The first file of a table, by its name within the folder (`sales/part0.parquet`)."""
    folder = source.allowed_directories[0].as_posix() + "/"
    _, after = source.sql_from.split(folder, 1)
    return after.split("'", 1)[0]


@dataclass
class SummaryEnv:
    root: Path
    paths: DataPaths
    connections: ConnectionsService
    cache: ProfileCache
    summaries: DataSummaries
    hook: DataSummaryHook
    spy: ProfileSpy
    project_id: str

    @property
    def ctx(self) -> HookContext:
        return HookContext(project_id=self.project_id, agent="helper", trigger_text="hi")

    async def connect(self, name: str, files: dict[str, str | bytes]) -> Path:
        """A team folder (its name has a space) holding `files`, connected as `name`."""
        folder = self.root / f"team {name}"
        for relative, content in files.items():
            write(folder / relative, content)
        await self.connections.create(
            self.project_id, LOCAL_FOLDER_KIND, name, {"path": str(folder)}
        )
        return folder

    async def render(self) -> str:
        return await self.hook.render(self.ctx)


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")


def change(path: Path, content: str) -> None:
    """New content, and a modified time far from the old one (coarse clocks included)."""
    write(path, content)
    moment = time.time() + 120
    os.utime(path, (moment, moment))


def time_out_after_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every profile reaches its deadline right after the row count and the statistics, as
    a large table does in its per-column top-value scans."""

    def expires(self: object, deadline: Deadline) -> None:
        raise DeadlineExpired

    monkeypatch.setattr(f"{profile_module.__name__}._ProfileBuilder._top_values", expires)


OUT_OF_MEMORY = "Out of Memory Error: could not allocate block of size 256.0 KiB"


def out_of_memory_at(
    monkeypatch: pytest.MonkeyPatch, step: str = "_top_values", error: Exception | None = None
) -> None:
    """Every profile runs out of DuckDB's memory at `step` of `_ProfileBuilder`, as a large
    id column's top-value grouping does at the memory limit (Decision #432)."""

    def fails(self: object, deadline: Deadline) -> None:
        raise error or duckdb.OutOfMemoryException(OUT_OF_MEMORY)

    monkeypatch.setattr(f"{profile_module.__name__}._ProfileBuilder.{step}", fails)


def csv_rows(count: int) -> str:
    return "id,name\n" + "".join(f"{i},n{i}\n" for i in range(1, count + 1))


async def build_summary_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    monkeypatch: pytest.MonkeyPatch,
    project_id: str = "prj_sum1",
) -> SummaryEnv:
    await databases.project(project_id)
    connections = ConnectionsService(
        databases, SqliteEventLog(databases), KindRegistry([LocalFolderKind(paths.root)])
    )
    spy = ProfileSpy()
    monkeypatch.setattr(summaries_module, "profile_table", spy)
    cache = ProfileCache(paths)
    summaries = DataSummaries(connections, cache)
    return SummaryEnv(
        root=tmp_path,
        paths=paths,
        connections=connections,
        cache=cache,
        summaries=summaries,
        hook=DataSummaryHook(connections, summaries),
        spy=spy,
        project_id=project_id,
    )
