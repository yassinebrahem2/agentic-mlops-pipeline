from mlops_agents.config import ConfigError, RunMode, Settings
from mlops_agents.credentials.file_store import EncryptedFileStore
from mlops_agents.credentials.keyring_store import KeyringStore
from mlops_agents.credentials.keystore import KeyStore, KeyStoreError
from mlops_agents.paths import DataPaths

__all__ = ["KeyStore", "KeyStoreError", "open_keystore"]


def open_keystore(settings: Settings, paths: DataPaths) -> KeyStore:
    """The OS keyring locally; keys.enc in server mode, checked now so a wrong key stops startup."""
    if settings.mode is RunMode.SERVER:
        if settings.master_key is None:
            raise ConfigError("Server mode needs MLOPS_AGENTS_MASTER_KEY.")
        store = EncryptedFileStore(paths.keys_file, settings.master_key)
        store.check()
        return store
    return KeyringStore()
