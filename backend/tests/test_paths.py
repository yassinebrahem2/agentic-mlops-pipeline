from pathlib import Path

import pytest

from mlops_agents.ids import new_id
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


UNSAFE_IDS = ["../x", "a/b", "prj_..\\x", ""]


@pytest.mark.parametrize("invalid_id", UNSAFE_IDS)
def test_project_id_validation_rejects_unsafe_paths(tmp_path: Path, invalid_id: str) -> None:
    p = DataPaths(tmp_path)
    with pytest.raises(ValueError):
        p.project_dir(invalid_id)


def test_cache_dir_is_inside_the_project_and_not_made_by_ensure(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    p.ensure()
    assert p.cache_dir("prj_1") == tmp_path / "projects" / "prj_1" / "cache"
    assert not p.cache_dir("prj_1").exists()


@pytest.mark.parametrize("invalid_id", UNSAFE_IDS)
def test_cache_dir_rejects_unsafe_project_ids(tmp_path: Path, invalid_id: str) -> None:
    p = DataPaths(tmp_path)
    with pytest.raises(ValueError):
        p.cache_dir(invalid_id)


def test_project_id_validation_accepts_valid_ids(tmp_path: Path) -> None:
    p = DataPaths(tmp_path)
    assert p.project_dir("prj_1") == tmp_path / "projects" / "prj_1"
    real_id = new_id("prj")
    assert p.project_dir(real_id) == tmp_path / "projects" / real_id
