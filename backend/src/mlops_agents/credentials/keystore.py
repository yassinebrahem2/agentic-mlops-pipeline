from typing import Protocol


class KeyStoreError(RuntimeError):
    """The key store failed. Messages never contain a key."""


class KeyStore(Protocol):
    def get(self, ref: str) -> str | None: ...

    def set(self, ref: str, value: str) -> None: ...

    def delete(self, ref: str) -> None: ...
