from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from mlops_agents.config import APP_NAME
from mlops_agents.credentials.keystore import KeyStoreError

SERVICE = APP_NAME


class KeyringBackendLike(Protocol):
    def get_password(self, service: str, username: str, /) -> str | None: ...

    def set_password(self, service: str, username: str, password: str, /) -> None: ...

    def delete_password(self, service: str, username: str, /) -> None: ...


class KeyringStore:
    """Keys in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service)."""

    def __init__(self, backend: KeyringBackendLike | None = None) -> None:
        self._backend: KeyringBackendLike = (
            backend if backend is not None else keyring.get_keyring()
        )

    def get(self, ref: str) -> str | None:
        try:
            return self._backend.get_password(SERVICE, ref)
        except KeyringError as exc:
            raise KeyStoreError(
                f"The system keyring could not be read ({type(exc).__name__})."
            ) from None

    def set(self, ref: str, value: str) -> None:
        try:
            self._backend.set_password(SERVICE, ref, value)
            stored = self._backend.get_password(SERVICE, ref)
        except KeyringError as exc:
            raise KeyStoreError(
                f"The system keyring refused to store the key ({type(exc).__name__})."
            ) from None
        if stored != value:
            raise KeyStoreError(
                "The system keyring did not keep the key. Check that a keyring service is running."
            )

    def delete(self, ref: str) -> None:
        try:
            self._backend.delete_password(SERVICE, ref)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise KeyStoreError(
                f"The system keyring could not delete the key ({type(exc).__name__})."
            ) from None
