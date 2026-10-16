import sqlite3
from pathlib import Path

from sqlalchemy import MetaData

from mlops_agents.storage import tables
from mlops_agents.storage.migrate import migrate


def _assert_matches(db: Path, metadata: MetaData) -> None:
    conn = sqlite3.connect(db)
    try:
        for table in metadata.sorted_tables:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table.name})")]
            assert columns == [c.name for c in table.columns], table.name
    finally:
        conn.close()


def test_core_tables_match_migrations(tmp_path: Path) -> None:
    migrate(tmp_path / "app.db", "app", tmp_path / "bk")
    migrate(tmp_path / "project.db", "project", tmp_path / "bk")
    _assert_matches(tmp_path / "app.db", tables.app_metadata)
    _assert_matches(tmp_path / "project.db", tables.project_metadata)
