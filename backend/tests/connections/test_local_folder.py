"""Tests for LocalFolderKind and its path checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mlops_agents.connections.contract import LocalFolderSettings
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.errors import DomainError


def _make_link_or_junction(target: Path, link: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
            )
            return res.returncode == 0
        return False


def test_data_folder_containment_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    inside = home / "sub"
    inside.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    kind = LocalFolderKind(home)

    # 1. Folder inside home
    with pytest.raises(DomainError) as err_inside:
        kind.validate(LocalFolderSettings(path=str(inside)), None)
    assert err_inside.value.code == "connection_invalid"
    assert err_inside.value.message == f"{inside} is inside the application data folder."

    # 2. Home itself
    with pytest.raises(DomainError) as err_self:
        kind.validate(LocalFolderSettings(path=str(home)), None)
    assert err_self.value.code == "connection_invalid"
    assert err_self.value.message == f"{home} is the application data folder."

    # 3. Folder containing home
    with pytest.raises(DomainError) as err_containing:
        kind.validate(LocalFolderSettings(path=str(tmp_path)), None)
    assert err_containing.value.code == "connection_invalid"
    assert err_containing.value.message == f"{tmp_path} contains the application data folder."


def test_relative_and_unc_paths_refused(tmp_path: Path) -> None:
    kind = LocalFolderKind(tmp_path / "home")

    # Relative path
    with pytest.raises(DomainError) as err_rel:
        kind.validate(LocalFolderSettings(path="relative/path"), None)
    assert err_rel.value.code == "connection_invalid"
    assert "Give the full path of the folder." in err_rel.value.message

    # UNC path (pure-path test on every platform)
    with pytest.raises(DomainError) as err_unc:
        kind.validate(LocalFolderSettings(path=r"\\host\share\x"), None)
    assert err_unc.value.code == "connection_invalid"
    assert "Network and UNC paths are not supported." in err_unc.value.message


def test_link_or_junction_into_home_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    secret_target = home / "secret"
    secret_target.mkdir()

    link = data / "link_to_home"
    if not _make_link_or_junction(secret_target, link):
        pytest.skip("Symlinks and junctions not supported in this environment")

    kind = LocalFolderKind(home)

    # Refused at creation/validation
    with pytest.raises(DomainError) as err:
        kind.validate(LocalFolderSettings(path=str(link)), None)
    assert err.value.code == "connection_invalid"

    # Retargeted link after creation:
    # First create valid target outside home
    valid_target = tmp_path / "valid_target"
    valid_target.mkdir()
    retargetable = data / "retargetable"
    if not _make_link_or_junction(valid_target, retargetable):
        pytest.skip("Symlinks/junctions failed")

    settings = LocalFolderSettings(path=str(retargetable))
    kind.validate(settings, None)

    # Retarget into home
    if retargetable.is_symlink():
        retargetable.unlink()
    elif os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(retargetable)], capture_output=True)
    assert _make_link_or_junction(secret_target, retargetable)

    # Now tables and sandbox_mount must refuse it
    with pytest.raises(DomainError) as err_tables:
        kind.tables(settings)
    assert err_tables.value.code == "connection_invalid"

    with pytest.raises(DomainError) as err_mount:
        kind.sandbox_mount("test", settings)
    assert err_mount.value.code == "connection_invalid"


def test_missing_file_and_unreadable_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    kind = LocalFolderKind(home)

    # Missing path
    with pytest.raises(DomainError) as err_missing:
        kind.validate(LocalFolderSettings(path=str(tmp_path / "does_not_exist")), None)
    assert err_missing.value.code == "connection_invalid"
    assert "does not exist" in err_missing.value.message

    # File instead of directory
    a_file = tmp_path / "regular_file.txt"
    a_file.write_text("hello", encoding="utf-8")
    with pytest.raises(DomainError) as err_file:
        kind.validate(LocalFolderSettings(path=str(a_file)), None)
    assert err_file.value.code == "connection_invalid"
    assert "not a directory" in err_file.value.message

    # Unreadable folder (POSIX chmod 000, skipped on Windows)
    if os.name != "nt":
        unreadable = tmp_path / "unreadable"
        unreadable.mkdir()
        unreadable.chmod(0o000)
        try:
            with pytest.raises(DomainError) as err_unreadable:
                kind.validate(LocalFolderSettings(path=str(unreadable)), None)
            assert err_unreadable.value.code == "connection_invalid"
            assert "not allowed to read" in err_unreadable.value.message
        finally:
            unreadable.chmod(0o755)


def test_invalid_characters_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    kind = LocalFolderKind(home)

    # Path containing comma
    with pytest.raises(DomainError) as err_comma:
        kind.validate(LocalFolderSettings(path=str(tmp_path / "foo,bar")), None)
    assert err_comma.value.code == "connection_invalid"

    # Path containing quotes
    with pytest.raises(DomainError) as err_quote:
        kind.validate(LocalFolderSettings(path=str(tmp_path / 'foo"bar')), None)
    assert err_quote.value.code == "connection_invalid"

    # POSIX path containing colon
    if os.name != "nt":
        with pytest.raises(DomainError) as err_colon:
            kind.validate(LocalFolderSettings(path=str(tmp_path / "foo:bar")), None)
        assert err_colon.value.code == "connection_invalid"


def test_sandbox_mount(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    kind = LocalFolderKind(home)
    mount = kind.sandbox_mount("hotel-data", LocalFolderSettings(path=str(data)))
    assert mount is not None
    assert mount.host_path == data.resolve()
    assert mount.container_path == "/data/hotel-data"
