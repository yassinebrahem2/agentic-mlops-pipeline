from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from mlops_agents.paths import DataPaths
from mlops_agents.storage.sqlite import connect_sync


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _vacuum_into(db_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sync(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()


def _prune(entries: list[Path], keep: int) -> None:
    if len(entries) <= keep:
        return
    for old in sorted(entries)[:-keep]:
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()


def backup_db(db_path: Path, backup_dir: Path, keep: int = 10) -> Path:
    """A consistent copy of a database, safe while the app is writing to it."""
    dest = backup_dir / f"{_stamp()}.db"
    _vacuum_into(db_path, dest)
    _prune(list(backup_dir.glob("*.db")), keep)
    return dest


def snapshot_all(paths: DataPaths, keep: int = 10) -> Path:
    snapshots = paths.backups / "snapshots"
    dest = snapshots / _stamp()
    dest.mkdir(parents=True, exist_ok=True)
    if paths.app_db.exists():
        _vacuum_into(paths.app_db, dest / "app.db")
    for project_db in sorted(paths.projects.glob("*/project.db")):
        _vacuum_into(project_db, dest / "projects" / f"{project_db.parent.name}.db")
    _prune([p for p in snapshots.iterdir() if p.is_dir()], keep)
    return dest
