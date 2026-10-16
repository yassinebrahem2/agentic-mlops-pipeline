from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "mlops-agents"
ENV_PREFIX = "MLOPS_AGENTS_"
DEFAULT_PORT = 8765


class RunMode(StrEnum):
    LOCAL = "local"
    SERVER = "server"


class ConfigError(Exception):
    """Startup configuration is invalid. The message is shown to the user as is."""


@dataclass(frozen=True)
class Settings:
    mode: RunMode
    data_dir: Path
    host: str
    port: int
    password: str | None
    master_key: str | None
    access_token: str | None
    allowed_hosts: tuple[str, ...]
    dev_origins: tuple[str, ...]
    trusted_proxies: tuple[str, ...]


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(ENV_PREFIX + name, "").strip()
    return value or None


def _csv(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def default_data_dir() -> Path:
    # Non-roaming on Windows: SQLite must stay on local disk.
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))


def load_settings(
    mode: RunMode, port: int = DEFAULT_PORT, env: Mapping[str, str] | None = None
) -> Settings:
    source = os.environ if env is None else env
    home = _get(source, "HOME")
    data_dir = Path(home) if home else default_data_dir()
    allowed_hosts = _csv(_get(source, "ALLOWED_HOSTS"))
    password: str | None = None
    master_key: str | None = None
    if mode is RunMode.SERVER:
        password = _get(source, "PASSWORD")
        master_key = _get(source, "MASTER_KEY")
        missing = [
            name
            for name, value in (
                ("MLOPS_AGENTS_PASSWORD", password),
                ("MLOPS_AGENTS_MASTER_KEY", master_key),
                ("MLOPS_AGENTS_ALLOWED_HOSTS", ",".join(allowed_hosts)),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Server mode needs these environment variables: "
                + ", ".join(missing)
                + ". Set them and start again."
            )
    return Settings(
        mode=mode,
        data_dir=data_dir,
        host="0.0.0.0" if mode is RunMode.SERVER else "127.0.0.1",  # noqa: S104 -- server mode sits behind a reverse proxy
        port=port,
        password=password,
        master_key=master_key,
        access_token=_get(source, "ACCESS_TOKEN"),
        allowed_hosts=allowed_hosts,
        dev_origins=_csv(_get(source, "DEV_ORIGINS")),
        trusted_proxies=_csv(_get(source, "TRUSTED_PROXIES")),
    )
