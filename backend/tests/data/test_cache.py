"""The profile cache: keys, single flight, cleanup, eviction and atomic writes."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from mlops_agents.data import cache as cache_module
from mlops_agents.data.cache import ProfileCache
from mlops_agents.data.constants import PROFILE_TIMEOUT_S
from mlops_agents.data.profile import ColumnProfile, TableProfile
from mlops_agents.paths import DataPaths

PROJECT = "prj_1"


def make_profile(
    *, row_count: int = 3, timed_out: bool = False, timeout_s: float = PROFILE_TIMEOUT_S
) -> TableProfile:
    return TableProfile(
        row_count=row_count,
        columns=[
            ColumnProfile(
                name="id", type="BIGINT", nulls=0, distinct=3, min="1", max="3", top_values=[]
            )
        ],
        more_columns=0,
        sample_columns=["id"],
        sample=[["1"], ["2"], ["3"]],
        timed_out=timed_out,
        timeout_s=timeout_s,
    )


class Counting:
    """A `compute` that counts its calls and yields to the loop before returning."""

    def __init__(self, profile: TableProfile | None = None) -> None:
        self.calls = 0
        self.profile = profile or make_profile()

    async def __call__(self) -> TableProfile:
        self.calls += 1
        for _ in range(5):
            await asyncio.sleep(0)
        return self.profile


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "home")


@pytest.fixture
def cache(paths: DataPaths) -> ProfileCache:
    return ProfileCache(paths)


async def fetch(
    cache: ProfileCache,
    compute: Callable[[], Awaitable[TableProfile]],
    *,
    connection: str = "con_1",
    table: str = "people",
    version: str = "v1",
    sql_from: str = "read_csv(['/data/people.csv'])",
) -> TableProfile:
    return await cache.get_or_compute(PROJECT, connection, table, version, sql_from, compute)


def entries(paths: DataPaths) -> list[Path]:
    folder = paths.cache_dir(PROJECT)
    return sorted(folder.glob("*.json")) if folder.exists() else []


def all_files(paths: DataPaths) -> list[Path]:
    folder = paths.cache_dir(PROJECT)
    return sorted(p for p in folder.iterdir()) if folder.exists() else []


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def set_mtime(path: Path, seconds_ago: float) -> None:
    moment = time.time() - seconds_ago
    os.utime(path, (moment, moment))


# Keys


async def test_a_second_call_with_the_same_key_is_a_hit(
    cache: ProfileCache, paths: DataPaths
) -> None:
    compute = Counting()
    first = await fetch(cache, compute)
    second = await fetch(cache, compute)
    assert compute.calls == 1
    assert first == second == compute.profile
    assert len(entries(paths)) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"version": "v2"},
        {"table": "scores"},
        {"connection": "con_2"},
        {"sql_from": "read_csv(['/other/people.csv'])"},
    ],
)
async def test_a_changed_key_part_computes_again(
    cache: ProfileCache, changed: dict[str, str]
) -> None:
    compute = Counting()
    await fetch(cache, compute)
    await fetch(cache, compute, **changed)
    assert compute.calls == 2


async def test_a_changed_format_version_computes_again(
    cache: ProfileCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute = Counting()
    await fetch(cache, compute)
    monkeypatch.setattr(cache_module, "SUMMARY_FORMAT_VERSION", 999)
    await fetch(cache, compute)
    assert compute.calls == 2


async def test_the_file_name_is_two_hashes_and_holds_the_profile(
    cache: ProfileCache, paths: DataPaths
) -> None:
    compute = Counting()
    await fetch(cache, compute)
    (entry,) = entries(paths)
    prefix, suffix = entry.stem.split("-")
    assert len(prefix) == len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in prefix + suffix)
    assert TableProfile.model_validate_json(entry.read_bytes()) == compute.profile


@pytest.mark.parametrize(
    "table",
    ["..", "a\\b", "a/b", "c:x", "CON", "aux.csv", "t" * 300, 'it\'s "quoted"\nnewline'],
)
async def test_hostile_table_names_stay_inside_the_cache_folder(
    cache: ProfileCache, paths: DataPaths, tmp_path: Path, table: str
) -> None:
    compute = Counting()
    await fetch(cache, compute, table=table)
    await fetch(cache, compute, table=table)
    assert compute.calls == 1
    written = files_under(tmp_path)
    assert written == entries(paths)
    assert all(
        p.parent == paths.cache_dir(PROJECT)
        and len(p.name) == len("0" * 16 + "-" + "0" * 16 + ".json")
        for p in written
    )


# Single flight


async def test_concurrent_calls_for_one_key_compute_once(cache: ProfileCache) -> None:
    compute = Counting()
    results = await asyncio.gather(*(fetch(cache, compute) for _ in range(4)))
    assert compute.calls == 1
    assert all(result == compute.profile for result in results)


async def test_calls_for_different_keys_run_in_parallel(cache: ProfileCache) -> None:
    started = {"people": asyncio.Event(), "scores": asyncio.Event()}

    def waits_for(own: str, other: str) -> Callable[[], Awaitable[TableProfile]]:
        async def compute() -> TableProfile:
            started[own].set()
            await started[other].wait()
            return make_profile()

        return compute

    await asyncio.wait_for(
        asyncio.gather(
            fetch(cache, waits_for("people", "scores"), table="people"),
            fetch(cache, waits_for("scores", "people"), table="scores"),
        ),
        timeout=5,
    )


# Cleanup and eviction


async def test_a_changed_data_version_removes_that_tables_old_entry_only(
    cache: ProfileCache, paths: DataPaths
) -> None:
    await fetch(cache, Counting(), table="people", version="v1")
    await fetch(cache, Counting(), table="scores", version="v1")
    before = entries(paths)
    await fetch(cache, Counting(), table="people", version="v2")
    after = entries(paths)
    assert len(after) == 2
    (people_v1,) = [p for p in before if p not in after]
    (people_v2,) = [p for p in after if p not in before]
    assert people_v1.stem.split("-")[0] == people_v2.stem.split("-")[0]
    compute = Counting()
    await fetch(cache, compute, table="scores", version="v1")
    assert compute.calls == 0


async def two_entries_under_a_cap_of_two(paths: DataPaths) -> tuple[ProfileCache, Path, Path, int]:
    """Entries a (older) and b in a cache whose cap holds two entries and not three."""
    probe = DataPaths(paths.root.parent / "probe")
    await ProfileCache(probe).get_or_compute(PROJECT, "con_x", "t", "v", "s", Counting())
    (sized,) = entries(probe)
    size = sized.stat().st_size
    cap = 2 * size + size // 2
    cache = ProfileCache(paths, cap_bytes=cap)
    await fetch(cache, Counting(), table="a")
    (a,) = entries(paths)
    await fetch(cache, Counting(), table="b")
    (b,) = [p for p in entries(paths) if p != a]
    set_mtime(a, 200)
    set_mtime(b, 100)
    return cache, a, b, cap


async def test_eviction_removes_the_least_recently_used_first(paths: DataPaths) -> None:
    cache, a, b, cap = await two_entries_under_a_cap_of_two(paths)
    await fetch(cache, Counting(), table="c")
    remaining = entries(paths)
    assert a not in remaining
    assert b in remaining
    assert len(remaining) == 2
    assert sum(p.stat().st_size for p in remaining) <= cap


async def test_a_hit_protects_an_entry_from_eviction(paths: DataPaths) -> None:
    cache, a, b, cap = await two_entries_under_a_cap_of_two(paths)
    hit = Counting()
    await fetch(cache, hit, table="a")
    assert hit.calls == 0
    await fetch(cache, Counting(), table="c")
    remaining = entries(paths)
    assert a in remaining
    assert b not in remaining
    assert sum(p.stat().st_size for p in remaining) <= cap


async def test_the_new_entry_survives_a_cap_below_its_size(paths: DataPaths) -> None:
    cache = ProfileCache(paths, cap_bytes=1)
    await fetch(cache, Counting(), table="a")
    await fetch(cache, Counting(), table="b")
    (only,) = entries(paths)
    compute = Counting()
    await fetch(cache, compute, table="b")
    assert compute.calls == 0
    assert only.exists()


# Corruption and atomic writes


async def test_a_corrupt_file_is_recomputed_and_replaced(
    cache: ProfileCache, paths: DataPaths
) -> None:
    await fetch(cache, Counting())
    (entry,) = entries(paths)
    entry.write_bytes(b"{not json\xff")
    compute = Counting(make_profile(row_count=7))
    result = await fetch(cache, compute)
    assert compute.calls == 1
    assert result.row_count == 7
    assert TableProfile.model_validate_json(entry.read_bytes()).row_count == 7
    assert all_files(paths) == [entry]


async def test_a_compute_that_raises_leaves_no_file(cache: ProfileCache, paths: DataPaths) -> None:
    async def fails() -> TableProfile:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await fetch(cache, fails)
    assert all_files(paths) == []


async def test_a_failed_replace_leaves_no_partial_file(
    cache: ProfileCache, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_replace(self: Path, target: Path) -> Path:
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "replace", broken_replace)
    with pytest.raises(OSError, match="disk gone"):
        await fetch(cache, Counting())
    assert all_files(paths) == []


# Timed-out profiles


async def test_a_timed_out_profile_under_the_full_timeout_is_stored(cache: ProfileCache) -> None:
    compute = Counting(make_profile(timed_out=True, timeout_s=PROFILE_TIMEOUT_S))
    await fetch(cache, compute)
    second = await fetch(cache, compute)
    assert compute.calls == 1
    assert second.timed_out
    await fetch(cache, compute, version="v2")
    assert compute.calls == 2


async def test_a_timed_out_profile_under_a_shorter_budget_is_not_stored(
    cache: ProfileCache, paths: DataPaths
) -> None:
    compute = Counting(make_profile(timed_out=True, timeout_s=PROFILE_TIMEOUT_S / 2))
    first = await fetch(cache, compute)
    assert first.timed_out
    assert entries(paths) == []
    await fetch(cache, compute)
    assert compute.calls == 2


async def test_a_complete_profile_under_a_shorter_budget_is_stored(cache: ProfileCache) -> None:
    compute = Counting(make_profile(timed_out=False, timeout_s=PROFILE_TIMEOUT_S / 2))
    await fetch(cache, compute)
    await fetch(cache, compute)
    assert compute.calls == 1


# Files held open or already gone (Windows refuses to delete or replace an open file)


async def test_replacing_an_entry_another_handle_reads(
    cache: ProfileCache, paths: DataPaths
) -> None:
    await fetch(cache, Counting())
    (entry,) = entries(paths)
    entry.write_bytes(b"garbage")
    compute = Counting(make_profile(row_count=9))
    with entry.open("rb"):
        result = await fetch(cache, compute)
    assert result.row_count == 9
    assert not [p for p in all_files(paths) if p.suffix != ".json"]
    if sys.platform != "win32":
        assert TableProfile.model_validate_json(entry.read_bytes()).row_count == 9


async def test_cleanup_tolerates_an_open_old_entry(cache: ProfileCache, paths: DataPaths) -> None:
    await fetch(cache, Counting(), version="v1")
    (old,) = entries(paths)
    with old.open("rb"):
        await fetch(cache, Counting(), version="v2")
    assert len([p for p in entries(paths) if p != old]) == 1
    if sys.platform != "win32":
        assert not old.exists()


async def test_eviction_tolerates_an_open_entry(paths: DataPaths) -> None:
    cache = ProfileCache(paths, cap_bytes=1)
    await fetch(cache, Counting(), table="a")
    (a,) = entries(paths)
    with a.open("rb"):
        await fetch(cache, Counting(), table="b")
    assert len([p for p in entries(paths) if p != a]) == 1
    if sys.platform != "win32":
        assert not a.exists()


async def test_cleanup_tolerates_an_entry_already_gone(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ProfileCache(paths, cap_bytes=1)
    await fetch(cache, Counting(), table="a")
    (a,) = entries(paths)
    real_unlink = Path.unlink

    def gone_first(self: Path, missing_ok: bool = False) -> None:
        if self == a:
            real_unlink(self)
            raise FileNotFoundError(str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", gone_first)
    await fetch(cache, Counting(), table="b")
    assert not a.exists()
    assert len(entries(paths)) == 1
