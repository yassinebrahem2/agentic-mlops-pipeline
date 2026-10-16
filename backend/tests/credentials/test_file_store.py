import os
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_file_permissions_are_owner_only(tmp_path: Path) -> None:
    import stat

    path = tmp_path / "keys.enc"
    store = EncryptedFileStore(path, "master")
    store.set("a", "b")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_damaged_payload_with_non_string_value(tmp_path: Path) -> None:
    import base64
    import json
    import os

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    from mlops_agents.credentials.file_store import MAGIC

    path = tmp_path / "keys.enc"
    salt = os.urandom(16)
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    fernet = Fernet(base64.urlsafe_b64encode(kdf.derive(b"master")))
    token = fernet.encrypt(json.dumps({"a": 123}).encode("utf-8"))
    path.write_bytes(MAGIC + salt + token)
    with pytest.raises(KeyStoreError, match=re.escape("keys.enc is damaged")):
        EncryptedFileStore(path, "master").check()
