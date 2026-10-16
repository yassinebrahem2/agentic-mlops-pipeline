from pathlib import Path

from mlops_agents.paths import DataPaths


def test_layout(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    assert p.app_db == tmp_path / "app.db"
    assert p.keys_file == tmp_path / "keys.enc"
    assert p.project_db("prj_1") == tmp_path / "projects" / "prj_1" / "project.db"
    assert p.repo_dir("prj_1") == tmp_path / "projects" / "prj_1" / "repo"


def test_ensure_creates_folders(tmp_path: Path) -> None:
    p = DataPaths(tmp_path / "data")
    p.ensure()
    assert p.projects.is_dir()
    assert p.backups.is_dir()
    assert p.bin.is_dir()
