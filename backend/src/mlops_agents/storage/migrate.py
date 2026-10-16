from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from mlops_agents.clock import now_iso
from mlops_agents.storage.backup import backup_db
from mlops_agents.storage.sqlite import connect_sync

DbKind = Literal["app", "project"]
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(kind: DbKind, source: Path | None = None) -> list[Migration]:
    folder: Traversable = (
        source if source is not None else files("mlops_agents.storage").joinpath("migrations", kind)
    )
    found: list[Migration] = []
    for entry in folder.iterdir():
        match = _NAME.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"{kind} migrations must be numbered 0001, 0002, ... without gaps; "
                f"found {migration.name}"
            )
    return found


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def migrate(
    db_path: Path, kind: DbKind, backup_dir: Path, *, source: Path | None = None
) -> list[int]:
    """Apply pending migrations, each in its own transaction. Returns the versions applied."""
    migrations = load_migrations(kind, source)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sync(db_path)
    try:
        current = _current_version(conn)
        latest = migrations[-1].version if migrations else 0
        if current > latest:
            raise MigrationError(
                f"{db_path.name} was created by a newer version of the app "
                f"(schema {current}, this app knows {latest}). Update the app."
            )
        pending = [m for m in migrations if m.version > current]
        if not pending:
            return []
        backup_path: Path | None = None
        if current > 0:
            backup_path = backup_db(db_path, backup_dir)
        for migration in pending:
            try:
                # executescript leaves the transaction opened by BEGIN open on success.
                conn.executescript("BEGIN;\n" + migration.sql)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (migration.version, now_iso()),
                )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if backup_path is not None:
                    advice = (
                        f"A backup was taken before migrating at {backup_path}; "
                        "the database can be restored from it."
                    )
                else:
                    advice = "Report the error with the database file kept as is."
                raise MigrationError(f"Migration {migration.name} failed: {exc}. {advice}") from exc
        return [m.version for m in pending]
    finally:
        conn.close()
