from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from mlops_agents.credentials.keystore import KeyStoreError

MAGIC = b"MLAK1"
SALT_LEN = 16


class EncryptedFileStore:
    """Keys in one encrypted JSON document, for server mode where no OS keyring exists.

    File layout: MAGIC, a random scrypt salt, then a Fernet token. The Fernet key is derived
    from MLOPS_AGENTS_MASTER_KEY with scrypt. Writes replace the file atomically.
    """

    def __init__(self, path: Path, master_key: str) -> None:
        self._path = path
        self._master_key = master_key
        self._lock = threading.Lock()
        self._salt = b""
        self._fernet: Fernet | None = None
        self._data: dict[str, str] = {}

    def _derive(self, salt: bytes) -> Fernet:
        kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
        return Fernet(base64.urlsafe_b64encode(kdf.derive(self._master_key.encode("utf-8"))))

    def _load(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        if not self._path.exists():
            self._salt = os.urandom(SALT_LEN)
            self._fernet = self._derive(self._salt)
            self._data = {}
            return self._fernet
        raw = self._path.read_bytes()
        header = len(MAGIC) + SALT_LEN
        if not raw.startswith(MAGIC) or len(raw) <= header:
            raise KeyStoreError(f"{self._path.name} is not a key file written by this app.")
        salt = raw[len(MAGIC) : header]
        fernet = self._derive(salt)
        try:
            payload = fernet.decrypt(raw[header:])
        except InvalidToken:
            raise KeyStoreError(
                "MLOPS_AGENTS_MASTER_KEY does not unlock keys.enc. Start with the master key that "
                "was used when the keys were saved, or delete keys.enc and add your keys again."
            ) from None
        loaded: object = json.loads(payload)
        if not isinstance(loaded, dict):
            raise KeyStoreError(f"{self._path.name} is damaged.")
        data: dict[str, str] = {}
        for k, v in cast(dict[object, object], loaded).items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise KeyStoreError(f"{self._path.name} is damaged.")
            data[k] = v

        self._data = data
        self._salt = salt
        self._fernet = fernet
        return fernet

    def _save(self, fernet: Fernet) -> None:
        token = fernet.encrypt(json.dumps(self._data).encode("utf-8"))
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(MAGIC + self._salt + token)
        tmp.chmod(0o600)
        tmp.replace(self._path)

    def check(self) -> None:
        with self._lock:
            self._load()

    def get(self, ref: str) -> str | None:
        with self._lock:
            self._load()
            return self._data.get(ref)

    def set(self, ref: str, value: str) -> None:
        with self._lock:
            fernet = self._load()
            self._data[ref] = value
            self._save(fernet)

    def delete(self, ref: str) -> None:
        with self._lock:
            fernet = self._load()
            if self._data.pop(ref, None) is not None:
                self._save(fernet)
