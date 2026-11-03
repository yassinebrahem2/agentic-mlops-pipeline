import pytest

from mlops_agents.credentials.keyring_store import (
    KEYRING_MAX_CHARS,
    SERVICE,
    KeyringStore,
)
from mlops_agents.credentials.keystore import KeyStoreError, KeyTooLongError


class MemoryBackend:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


class DroppingBackend(MemoryBackend):
    def set_password(self, service: str, username: str, password: str) -> None:
        return None


def test_round_trip_uses_app_service() -> None:
    backend = MemoryBackend()
    store = KeyringStore(backend)
    store.set("provider/x", "secret-value")
    assert store.get("provider/x") == "secret-value"
    assert (SERVICE, "provider/x") in backend.data
    store.delete("provider/x")
    assert store.get("provider/x") is None
    store.delete("provider/x")  # deleting twice is fine


def test_silently_dropped_write_is_an_error() -> None:
    store = KeyringStore(DroppingBackend())
    with pytest.raises(KeyStoreError, match="did not keep the key") as err:
        store.set("provider/x", "secret-value")
    assert "secret-value" not in str(err.value)


class NoKeyringBackend:
    def get_password(self, service: str, username: str) -> str | None:
        from keyring.errors import NoKeyringError

        raise NoKeyringError("No recommended backend")

    def set_password(self, service: str, username: str, password: str) -> None:
        from keyring.errors import NoKeyringError

        raise NoKeyringError("No recommended backend")

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import NoKeyringError

        raise NoKeyringError("No recommended backend")


def test_no_keyring_error_explains_how_to_fix() -> None:
    store = KeyringStore(NoKeyringBackend())
    for action in (
        lambda: store.get("provider/x"),
        lambda: store.set("provider/x", "val"),
        lambda: store.delete("provider/x"),
    ):
        with pytest.raises(KeyStoreError) as err:
            action()
        msg = str(err.value)
        assert "NoKeyringError" in msg
        assert "On Linux start a Secret Service" in msg
        assert "MLOPS_AGENTS_MASTER_KEY" in msg


class FailingWriteBackend(MemoryBackend):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("OS stub bad data")


def test_non_keyring_error_on_write_raises_keystore_error() -> None:
    store = KeyringStore(FailingWriteBackend())
    with pytest.raises(KeyStoreError) as err:
        store.set("provider/x", "val")
    msg = str(err.value)
    assert "The system keyring refused to store the key (RuntimeError)." in msg
    assert "On Linux start a Secret Service" in msg


def test_key_longer_than_max_chars_is_refused_before_write() -> None:
    backend = MemoryBackend()
    store = KeyringStore(backend)
    too_long = "x" * (KEYRING_MAX_CHARS + 1)
    with pytest.raises(KeyTooLongError) as err:
        store.set("provider/x", too_long)
    assert isinstance(err.value, KeyStoreError)
    msg = str(err.value)
    assert "too long to be a key" in msg
    assert "Windows Credential Manager holds at most 1280 characters" in msg
    assert too_long not in msg
    assert backend.data == {}


def test_key_at_max_chars_is_stored() -> None:
    backend = MemoryBackend()
    store = KeyringStore(backend)
    exact = "k" * KEYRING_MAX_CHARS
    store.set("provider/x", exact)
    assert store.get("provider/x") == exact
