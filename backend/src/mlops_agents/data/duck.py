"""The locked DuckDB session.

DuckDB cannot make an allowed folder read-only: `COPY ... TO` and `ATTACH` inside it still
write (Decision #411). The lock-down keeps everything outside the folder out of reach and
extensions off; the boundary inside the folder is the SQL itself. A locked connection runs
only SQL this package builds, never text from an agent or a user, and every column name in
it goes through `quote_identifier`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import duckdb

_EXTENDED_LENGTH_PREFIX: Final = "\\\\?\\"
# An interrupt on an idle connection is lost, so an expired deadline repeats it this often.
_INTERRUPT_EVERY_S: Final = 0.01
DEADLINE_THREAD_NAME: Final = "duckdb-deadline"


def posix_directory(path: Path) -> str:
    """`path` resolved, as DuckDB compares it: forward slashes, no `\\\\?\\` prefix."""
    resolved = path.resolve()
    if str(resolved).startswith(_EXTENDED_LENGTH_PREFIX):
        resolved = Path(os.path.realpath(path))
    return resolved.as_posix()


def quote_identifier(name: str) -> str:
    """`name` as a SQL identifier: the one quoting of a column name from a team's file."""
    return '"' + name.replace('"', '""') + '"'


def locked_connection(allowed: Sequence[Path]) -> duckdb.DuckDBPyConnection:
    """A new in-memory connection that reads only under `allowed` and loads no extension.

    Security-critical: the four statements run in this order, the last one freezing the
    others. Do not reorder them. The time zone is UTC so that no text the profile computes
    depends on the host (Decision #419).
    """
    # The only settings given through the config dict; the lock itself must be SET statements.
    connection = duckdb.connect(
        ":memory:",
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
    )
    try:
        connection.execute(
            "SET allowed_directories = ?", [[posix_directory(path) for path in allowed]]
        )
        connection.execute("SET enable_external_access = false")
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection


class DeadlineExpired(Exception):
    """The deadline passed before the next query started."""


class Deadline:
    """Interrupts `connection` once `timeout_s` has passed, until `stop` is called.

    A watchdog thread waits for the deadline, then keeps interrupting, because an interrupt
    that lands between two queries is lost. `stop` joins the watchdog, so no interrupt ever
    reaches the connection after it: call it before closing the connection.
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection, timeout_s: float) -> None:
        self._connection = connection
        self._timeout_s = timeout_s
        self._done = threading.Event()
        self._expired = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watch, name=DEADLINE_THREAD_NAME, daemon=True
        )
        self._watchdog.start()

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def fetchall(self, sql: str) -> list[tuple[Any, ...]]:
        """Runs `sql`, built by this package, unless the deadline has already passed."""
        if self.expired:
            raise DeadlineExpired
        return self._connection.execute(sql).fetchall()

    def stop(self) -> None:
        self._done.set()
        self._watchdog.join()

    def _watch(self) -> None:
        if self._done.wait(self._timeout_s):
            return
        self._expired.set()
        while True:
            self._connection.interrupt()
            if self._done.wait(_INTERRUPT_EVERY_S):
                return
