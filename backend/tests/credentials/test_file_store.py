import re
from pathlib import Path

import pytest

from mlops_agents.credentials.file_store import EncryptedFileStore
from mlops_agents.credentials.keystore import KeyStoreError


def test_round_trip_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "keys.enc"
    store = EncryptedFileStore(path, "master")
    store.set("github/token", "ghp_secret")
    assert b"ghp_secret" not in path.read_bytes()
    again = EncryptedFileStore(path, "master")
    assert again.get("github/token") == "ghp_secret"
    again.delete("github/token")
    assert EncryptedFileStore(path, "master").get("github/token") is None


def test_wrong_master_key_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "keys.enc"
    EncryptedFileStore(path, "master").set("a", "b")
    with pytest.raises(KeyStoreError, match=re.escape("does not unlock keys.enc")):
        EncryptedFileStore(path, "other").check()


def test_foreign_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "keys.enc"
    path.write_bytes(b"not ours")
    with pytest.raises(KeyStoreError, match="not a key file"):
        EncryptedFileStore(path, "master").check()


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    store = EncryptedFileStore(tmp_path / "keys.enc", "master")
    store.set("a", "b")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keys.enc"]


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    store = EncryptedFileStore(tmp_path / "keys.enc", "master")
    store.check()
    assert store.get("anything") is None
    assert not (tmp_path / "keys.enc").exists()
