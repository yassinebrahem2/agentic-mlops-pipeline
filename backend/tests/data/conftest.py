"""A team's data folder (its name has a space) read through the real local folder kind."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from mlops_agents.connections.contract import DuckDbSource, LocalFolderSettings
from mlops_agents.connections.kinds.local_folder import LocalFolderKind


def sql_text(path: Path) -> str:
    """`path` as a quoted SQL string literal, for the unlocked setup connection only."""
    return "'" + path.resolve().as_posix().replace("'", "''") + "'"


def write_parquet(path: Path, select_sql: str) -> None:
    """A Parquet file made by an unlocked connection, as the team's own tools would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(":memory:") as setup:
        setup.execute(f"COPY ({select_sql}) TO {sql_text(path)} (FORMAT parquet)")


def folder_state(folder: Path) -> dict[str, tuple[int, str]]:
    """Every file under `folder`: its size and SHA-256."""
    return {
        p.relative_to(folder).as_posix(): (
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@dataclass
class TeamFolder:
    home: Path
    folder: Path

    def source(self, table: str) -> DuckDbSource:
        kind = LocalFolderKind(self.home)
        settings = LocalFolderSettings(path=str(self.folder))
        ref = next(t for t in kind.tables(settings) if t.name == table)
        return kind.duckdb_source(settings, ref)


@pytest.fixture
def team(tmp_path: Path) -> TeamFolder:
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "team data"
    folder.mkdir()
    return TeamFolder(home=home, folder=folder)


@pytest.fixture
def fixture_tables(team: TeamFolder) -> TeamFolder:
    """A CSV, a Parquet file, a JSON file, a JSON Lines file and a folder of Parquet parts."""
    folder = team.folder
    (folder / "people.csv").write_text("id,name\n1,ann\n2,bob\n3,cy\n", encoding="utf-8")
    write_parquet(
        folder / "scores.parquet", "SELECT range AS id, range * 1.5 AS score FROM range(4)"
    )
    (folder / "events.json").write_text(
        json.dumps([{"kind": "a", "n": 1}, {"kind": "b", "n": 2}]), encoding="utf-8"
    )
    (folder / "logs.jsonl").write_text(
        "\n".join(json.dumps({"level": "info", "n": i}) for i in range(5)) + "\n", encoding="utf-8"
    )
    for part in range(3):
        write_parquet(
            folder / "sales" / f"part{part}.parquet",
            f"SELECT range + {part * 10} AS amount FROM range(2)",
        )
    return team
