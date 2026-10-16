import pytest

from mlops_agents.credentials.keyring_store import SERVICE, KeyringStore
from mlops_agents.credentials.keystore import KeyStoreError


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
