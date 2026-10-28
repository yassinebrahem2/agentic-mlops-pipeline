import re
import sqlite3
from pathlib import Path

import pytest

from mlops_agents.storage.migrate import MigrationError, migrate


def _write(folder: Path, name: str, sql: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(sql, encoding="utf-8")


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_fresh_database_gets_every_migration(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "CREATE TABLE a (x INTEGER);")
    db = tmp_path / "x.db"
    assert migrate(db, "app", tmp_path / "bk", source=src) == [1]
    assert migrate(db, "app", tmp_path / "bk", source=src) == []
    assert not (tmp_path / "bk").exists()  # a brand-new database has nothing to back up


def test_pending_migration_backs_up_first(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "CREATE TABLE a (x INTEGER);")
    db = tmp_path / "x.db"
    migrate(db, "app", tmp_path / "bk", source=src)
    _write(src, "0002_more.sql", "CREATE TABLE b (y INTEGER);")
    assert migrate(db, "app", tmp_path / "bk", source=src) == [2]
    backups = list((tmp_path / "bk").glob("*.db"))
    assert len(backups) == 1
    assert "a" in _tables(backups[0])
    assert "b" not in _tables(backups[0])


def test_failed_migration_leaves_database_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "CREATE TABLE a (x INTEGER); CREATE TABLE a (x INTEGER);")
    db = tmp_path / "x.db"
    with pytest.raises(MigrationError, match=re.escape("0001_init.sql")):
        migrate(db, "app", tmp_path / "bk", source=src)
    assert "a" not in _tables(db)


def test_failed_migration_without_backup_tells_user_to_report(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "INVALID SQL;")
    db = tmp_path / "x.db"
    with pytest.raises(MigrationError) as exc_info:
        migrate(db, "app", tmp_path / "bk", source=src)
    msg = str(exc_info.value)
    assert "0001_init.sql failed" in msg
    assert "Report the error with the database file kept as is." in msg


def test_failed_migration_with_backup_names_backup_path(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "CREATE TABLE a (x INTEGER);")
    db = tmp_path / "x.db"
    migrate(db, "app", tmp_path / "bk", source=src)
    _write(src, "0002_bad.sql", "INVALID SQL;")
    with pytest.raises(MigrationError) as exc_info:
        migrate(db, "app", tmp_path / "bk", source=src)
    msg = str(exc_info.value)
    assert "0002_bad.sql failed" in msg
    assert "restored from it" in msg
    backups = list((tmp_path / "bk").glob("*.db"))
    assert len(backups) == 1
    assert str(backups[0]) in msg


def test_gap_in_numbering_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0002_late.sql", "CREATE TABLE a (x INTEGER);")
    with pytest.raises(MigrationError, match="without gaps"):
        migrate(tmp_path / "x.db", "app", tmp_path / "bk", source=src)


def test_newer_database_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "m"
    _write(src, "0001_init.sql", "CREATE TABLE a (x INTEGER);")
    db = tmp_path / "x.db"
    migrate(db, "app", tmp_path / "bk", source=src)
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("INSERT INTO schema_version VALUES (5, 'later')")
    finally:
        conn.close()
    with pytest.raises(MigrationError, match="newer version"):
        migrate(db, "app", tmp_path / "bk", source=src)


def test_packaged_migrations_apply(tmp_path: Path) -> None:
    assert migrate(tmp_path / "app.db", "app", tmp_path / "bk") == [1, 2]
    assert migrate(tmp_path / "project.db", "project", tmp_path / "bk") == [1, 2, 3, 4]
