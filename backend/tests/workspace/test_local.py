import errno
import os
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo, memory_project_secrets
from tests.integrations.test_github_auth import FakeGh


def _settings(paths: DataPaths, mode: RunMode = RunMode.LOCAL) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=mode)


def _service(
    paths: DataPaths, databases: Databases, keystore: MemoryKeyStore, mode: RunMode = RunMode.LOCAL
) -> WorkspaceService:
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    return WorkspaceService(
        _settings(paths, mode),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )


async def test_add_local_repo(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "demo", {"README.md": "# Demo"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    assert (project.name, project.source, project.status, project.default_branch) == (
        "demo",
        "local",
        "ok",
        "main",
    )
    assert project.id.startswith("prj_")
    assert svc.repo_root(project) == repo.resolve()
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(repo / "."))
    assert err.value.code == "project_exists"


async def test_not_a_repo_offers_git_init(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    svc = _service(paths, databases, keystore)
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(folder))
    assert err.value.code == "not_a_git_repo"
    assert err.value.detail == {"can_init": True}
    project = await svc.add_local(str(folder), init_git=True)
    assert project.status == "empty"
    assert (
        project.status_detail
        == "The repository has no committed files yet. Commit your files first."
    )


async def test_subfolder_of_a_repo_is_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "demo", {"src/a.py": "x = 1"}, git_env)
    with pytest.raises(DomainError) as err:
        await _service(paths, databases, keystore).add_local(str(repo / "src"))
    assert err.value.code == "not_repo_root"


async def test_server_mode_refuses_local_folders(
    tmp_path: Path, paths: DataPaths, databases: Databases, keystore: MemoryKeyStore
) -> None:
    svc = _service(paths, databases, keystore, RunMode.SERVER)
    with pytest.raises(DomainError) as err:
        await svc.add_local(str(tmp_path))
    assert err.value.code == "local_projects_disabled"
    with pytest.raises(DomainError):
        await svc.browse(None)


async def test_missing_folder_and_relink(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "demo", {"a.py": "x = 1"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    moved = tmp_path / "moved"
    shutil.move(repo, moved)
    opened = await svc.open(project.id)
    assert opened.status == "missing"
    relinked = await svc.relink(project.id, str(moved))
    assert relinked.status == "ok"
    assert relinked.local_path == str(moved.resolve())


async def test_remove_keeps_the_users_repo(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "demo", {"a.py": "x = 1"}, git_env)
    svc = _service(paths, databases, keystore)
    project = await svc.add_local(str(repo))
    await databases.project(project.id)  # the project database exists and is open
    await svc.remove(project.id)
    assert not paths.project_dir(project.id).exists()
    assert (repo / "a.py").is_file()
    assert await svc.list_projects() == []


async def test_browse_lists_folders(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    make_repo(tmp_path / "b-repo", {"a.py": "x"}, git_env)
    (tmp_path / "A-folder").mkdir()
    (tmp_path / "file.txt").write_text("x")
    listing = await _service(paths, databases, keystore).browse(str(tmp_path))
    names = [(e.name, e.is_git_repo) for e in listing.entries]
    assert ("A-folder", False) in names
    assert ("b-repo", True) in names
    assert all(e.name != "file.txt" for e in listing.entries)
    assert listing.parent == str(tmp_path.parent)


async def test_browse_unreadable_folder_names_the_os_reason(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_ready(self: Path) -> Iterator[Path]:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(Path, "iterdir", not_ready)
    with pytest.raises(DomainError) as err:
        await _service(paths, databases, keystore).browse(str(tmp_path))
    assert (err.value.code, err.value.status) == ("folder_unreadable", 409)
    assert err.value.message == (
        f"{tmp_path} could not be read (Input/output error). "
        "Check the drive or network path and try again."
    )


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_browse_without_a_path_lists_the_home_folder(
    text: str | None,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    listing = await _service(paths, databases, keystore).browse(text)
    assert listing.path == str(home)
    assert [e.name for e in listing.entries] == ["projects"]


@pytest.mark.skipif(os.name != "nt", reason="Windows path spellings; runs on the Windows CI leg")
async def test_windows_path_spellings_are_the_same_project(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "Demo", {"a.py": "x = 1"}, git_env)
    svc = _service(paths, databases, keystore)
    await svc.add_local(str(repo))
    text = str(repo)
    drive_flipped = (text[0].swapcase() + text[1:]) if text[1:2] == ":" else text
    for variant in (text.upper(), text.lower(), text.replace("\\", "/"), drive_flipped):
        with pytest.raises(DomainError) as err:
            await svc.add_local(variant)
        assert err.value.code == "project_exists"
