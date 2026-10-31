from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases


class MemoryKeyStore(KeyStore):
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.store.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.store[ref] = value

    def delete(self, ref: str) -> None:
        self.store.pop(ref, None)


@pytest.fixture
async def test_env(tmp_path: Path) -> AsyncIterator[tuple[Databases, MemoryKeyStore]]:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    try:
        yield databases, keystore
    finally:
        await databases.dispose()


async def test_integration_settings_defaults(test_env: tuple[Databases, MemoryKeyStore]) -> None:
    databases, keystore = test_env
    settings = IntegrationSettings(databases, keystore)

    discord = await settings.discord()
    assert not discord.enabled
    assert not discord.webhook_set

    email = await settings.email()
    assert not email.enabled
    assert not email.password_set
    assert email.port == 587

    public_url = await settings.public_url()
    assert public_url is None
    assert await settings.secret_values() == []


async def test_integration_settings_discord_roundtrip(
    test_env: tuple[Databases, MemoryKeyStore],
) -> None:
    databases, keystore = test_env
    settings = IntegrationSettings(databases, keystore)

    webhook_url = "https://discord.com/api/webhooks/123456789/my_secret_token"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    discord = await settings.discord()
    assert discord.enabled
    assert discord.webhook_set
    assert await settings.webhook_url() == webhook_url

    secrets = await settings.secret_values()
    assert webhook_url in secrets
    assert "my_secret_token" in secrets

    # Clear webhook
    await settings.clear_discord_webhook()
    discord2 = await settings.discord()
    assert not discord2.webhook_set
    assert await settings.webhook_url() is None


async def test_integration_settings_email_roundtrip(
    test_env: tuple[Databases, MemoryKeyStore],
) -> None:
    databases, keystore = test_env
    settings = IntegrationSettings(databases, keystore)

    await settings.set_email(
        enabled=True,
        host="smtp.example.com",
        port=465,
        user="alerts@example.com",
        sender="noreply@example.com",
        starttls=False,
        password="supersecretpassword",
    )

    email = await settings.email()
    assert email.enabled
    assert email.host == "smtp.example.com"
    assert email.port == 465
    assert email.user == "alerts@example.com"
    assert email.sender == "noreply@example.com"
    assert not email.starttls
    assert email.password_set
    assert await settings.email_password() == "supersecretpassword"

    secrets = await settings.secret_values()
    assert "supersecretpassword" in secrets

    # Clear password
    await settings.clear_email_password()
    email2 = await settings.email()
    assert not email2.password_set
    assert await settings.email_password() is None


async def test_integration_settings_public_url(test_env: tuple[Databases, MemoryKeyStore]) -> None:
    databases, keystore = test_env
    settings = IntegrationSettings(databases, keystore)

    await settings.set_public_url("https://app.mlops.example.com")
    assert await settings.public_url() == "https://app.mlops.example.com"
    assert settings.cached_public_url_host == "app.mlops.example.com"

    await settings.set_public_url(None)
    assert await settings.public_url() is None
    assert settings.cached_public_url_host is None
