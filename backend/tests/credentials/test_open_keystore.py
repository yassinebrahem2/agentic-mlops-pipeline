from pathlib import Path

from mlops_agents.config import RunMode, Settings
from mlops_agents.credentials import open_keystore
from mlops_agents.credentials.file_store import EncryptedFileStore
from mlops_agents.credentials.keyring_store import KeyringStore
from mlops_agents.paths import DataPaths


def _settings(mode: RunMode, tmp_path: Path) -> Settings:
    server = mode is RunMode.SERVER
    return Settings(
        mode=mode,
        data_dir=tmp_path,
        host="127.0.0.1",
        port=8765,
        password="pw" if server else None,
        master_key="mk" if server else None,
        access_token=None,
        allowed_hosts=("x.io",),
        dev_origins=(),
        trusted_proxies=(),
    )


def test_local_mode_uses_keyring(tmp_path: Path) -> None:
    store = open_keystore(_settings(RunMode.LOCAL, tmp_path), DataPaths(tmp_path))
    assert isinstance(store, KeyringStore)


def test_server_mode_uses_encrypted_file(tmp_path: Path) -> None:
    store = open_keystore(_settings(RunMode.SERVER, tmp_path), DataPaths(tmp_path))
    assert isinstance(store, EncryptedFileStore)
