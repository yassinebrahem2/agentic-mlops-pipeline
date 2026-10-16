import sqlite3
from pathlib import Path

from mlops_agents.paths import DataPaths
from mlops_agents.storage.backup import backup_db, snapshot_all
from mlops_agents.storage.migrate import migrate


def test_backup_is_a_consistent_copy(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('hello')")
    finally:
        conn.close()
    copy = backup_db(db, tmp_path / "bk")
    check = sqlite3.connect(copy)
    try:
        assert check.execute("SELECT v FROM t").fetchone() == ("hello",)
    finally:
        check.close()


def test_backup_keeps_only_the_newest(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    for _ in range(4):
        backup_db(db, tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("*.db"))) == 2


def test_snapshot_covers_app_and_projects(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    migrate(paths.app_db, "app", paths.backups)
    migrate(paths.project_db("prj_a"), "project", paths.backups)
    dest = snapshot_all(paths)
    assert (dest / "app.db").is_file()
    assert (dest / "projects" / "prj_a.db").is_file()
