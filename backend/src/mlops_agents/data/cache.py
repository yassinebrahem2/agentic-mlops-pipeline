"""The profile cache: one JSON file per table and data version, least recently used evicted.

It holds profiles (results), never copies of the tables.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from mlops_agents.data.constants import CACHE_CAP_BYTES, PROFILE_TIMEOUT_S, SUMMARY_FORMAT_VERSION
from mlops_agents.data.profile import TableProfile
from mlops_agents.paths import DataPaths

logger = logging.getLogger(__name__)

_ENTRY_SUFFIX: Final = ".json"
_TEMP_SUFFIX: Final = ".tmp"
_HASH_CHARS: Final = 16


class ProfileCache:
    """Profiles per project, keyed by connection, table, format version, data version and
    `sql_from`, so a connection pointed at another folder never hits its old entries."""

    def __init__(self, paths: DataPaths, cap_bytes: int = CACHE_CAP_BYTES) -> None:
        self._paths = paths
        self._cap_bytes = cap_bytes
        self._flights: dict[Path, _Flight] = {}

    async def get_or_compute(
        self,
        project_id: str,
        connection_id: str,
        table: str,
        data_version: str,
        sql_from: str,
        compute: Callable[[], Awaitable[TableProfile]],
    ) -> TableProfile:
        """The cached profile, or `compute()`'s, stored unless it was cut short by a budget
        below `PROFILE_TIMEOUT_S`. Concurrent calls for one key compute once."""
        prefix = _digest(connection_id, table)
        version = _digest(SUMMARY_FORMAT_VERSION, data_version, sql_from)
        entry = self._paths.cache_dir(project_id) / f"{prefix}-{version}{_ENTRY_SUFFIX}"
        flight = self._flights.setdefault(entry, _Flight())
        flight.users += 1
        try:
            async with flight.lock:
                cached = await asyncio.to_thread(_read, entry)
                if cached is not None:
                    return cached
                profile = await compute()
                if _storable(profile):
                    await asyncio.to_thread(self._store, entry, prefix, profile)
                return profile
        finally:
            flight.users -= 1
            if flight.users == 0:
                del self._flights[entry]

    def _store(self, entry: Path, prefix: str, profile: TableProfile) -> None:
        entry.parent.mkdir(parents=True, exist_ok=True)
        if not _write_atomically(entry, profile.model_dump_json().encode()):
            return
        for stale in entry.parent.glob(f"{prefix}-*{_ENTRY_SUFFIX}"):
            if stale != entry:  # another data version of the table: it can never hit again
                _remove(stale)
        self._evict(entry)

    def _evict(self, kept: Path) -> None:
        """Removes the least recently used entries, never `kept`, while over the cap."""
        sized: list[tuple[int, str, int]] = []
        for path in kept.parent.glob(f"*{_ENTRY_SUFFIX}"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            sized.append((stat.st_mtime_ns, path.name, stat.st_size))
        total = sum(size for _, _, size in sized)
        for _, name, size in sorted(sized):
            if total <= self._cap_bytes:
                return
            path = kept.parent / name
            if path != kept and _remove(path):
                total -= size


@dataclass
class _Flight:
    """The lock of one key and the number of calls holding or waiting for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def _digest(*parts: str | int) -> str:
    # JSON keeps the parts apart: ("a|b", "c") and ("a", "b|c") hash differently.
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:_HASH_CHARS]


def _storable(profile: TableProfile) -> bool:
    # A timed-out profile marks the data version as too slow only after a full-length run.
    return not profile.timed_out or profile.timeout_s >= PROFILE_TIMEOUT_S


def _read(entry: Path) -> TableProfile | None:
    try:
        raw = entry.read_bytes()
    except FileNotFoundError:
        return None
    try:
        profile = TableProfile.model_validate_json(raw)
    except ValueError:
        # Left by a crash or written in an older format: a miss, and the file goes.
        _remove(entry)
        return None
    # Use is the modified time: access times are unreliable on Windows.
    with contextlib.suppress(FileNotFoundError, PermissionError):
        os.utime(entry)  # evicted or held open meanwhile: the profile read is still valid
    return profile


def _write_atomically(entry: Path, data: bytes) -> bool:
    """Writes a temporary file in the same folder, then replaces `entry` with it. False when
    the platform refused the replace (Windows, while another handle has `entry` open)."""
    handle, temp_name = tempfile.mkstemp(dir=entry.parent, suffix=_TEMP_SUFFIX)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        temp.replace(entry)
    except PermissionError:
        _remove(temp)
        logger.warning("profile cache entry %s not replaced: it is held open", entry.name)
        return False
    except BaseException:
        _remove(temp)
        raise
    return True


def _remove(path: Path) -> bool:
    """Deletes `path`; True when it is gone (already or now), False when it is held open
    (Windows refuses to delete an open file; a later cleanup tries again)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except PermissionError:
        return False
    return True
