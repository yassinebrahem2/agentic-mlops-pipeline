from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx2
import pytest

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.notifications.channels.discord import DiscordChannel
from mlops_agents.notifications.constants import DISCORD_CONTENT_MAX
from mlops_agents.notifications.contract import ChannelError, Message
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord


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
def fake_discord() -> Iterator[RunningFakeDiscord]:
    state = FakeDiscordState()
    server = start_fake_discord(state)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
async def integration_env(tmp_path: Path) -> AsyncIterator[IntegrationSettings]:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    settings = IntegrationSettings(databases, keystore)
    try:
        yield settings
    finally:
        await databases.dispose()


async def test_discord_delivers_rendered_message_not_urgent(
    fake_discord: RunningFakeDiscord, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    webhook_url = f"{fake_discord.base_url}/api/webhooks/12345/my_secret_token"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    async with httpx2.AsyncClient() as http:
        channel = DiscordChannel(settings, http)
        msg = Message(
            title="Drift alert",
            severity="medium",
            cause="p-value exceeded 0.05",
            urgent=False,
        )
        await channel.send("user_123", msg)

    assert len(fake_discord.state.posts) == 1
    post = fake_discord.state.posts[0]
    assert "<@user_123>" not in post["content"]
    assert post["allowed_mentions"] == {"parse": []}
    assert "Drift alert" in post["content"]


async def test_discord_urgent_message_has_mention_and_safeguards_everyone(
    fake_discord: RunningFakeDiscord, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    webhook_url = f"{fake_discord.base_url}/api/webhooks/12345/my_secret_token"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    async with httpx2.AsyncClient() as http:
        channel = DiscordChannel(settings, http)
        # Cause contains dangerous @everyone ping
        msg = Message(
            title="Severe outage",
            severity="critical",
            cause="Database down! @everyone alert!",
            urgent=True,
        )
        await channel.send("user_999", msg)

    assert len(fake_discord.state.posts) == 1
    post = fake_discord.state.posts[0]
    # Mention prepended
    assert post["content"].startswith("<@user_999> ")
    # allowed_mentions only includes user_999, parse is empty so @everyone won't ping
    assert post["allowed_mentions"] == {"parse": [], "users": ["user_999"]}


async def test_discord_content_cut_at_max(
    fake_discord: RunningFakeDiscord, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    webhook_url = f"{fake_discord.base_url}/api/webhooks/12345/my_secret_token"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)

    async with httpx2.AsyncClient() as http:
        channel = DiscordChannel(settings, http)
        msg = Message(
            title="Long message",
            severity="low",
            lines=["A" * 3000],
            urgent=False,
        )
        await channel.send("user_123", msg)

    post = fake_discord.state.posts[0]
    assert len(post["content"]) <= DISCORD_CONTENT_MAX


async def test_discord_failing_webhook_scrubs_url_and_token(
    fake_discord: RunningFakeDiscord, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    secret_token = "ultra_sensitive_discord_token_xyz"
    webhook_url = f"{fake_discord.base_url}/api/webhooks/12345/{secret_token}"
    await settings.set_discord(enabled=True, webhook_url=webhook_url)
    fake_discord.state.fail_next(429)

    async with httpx2.AsyncClient() as http:
        channel = DiscordChannel(settings, http)
        msg = Message(title="Test", severity="low")
        with pytest.raises(ChannelError) as exc_info:
            await channel.send("user_123", msg)

        err_text = str(exc_info.value)
        assert secret_token not in err_text
        assert webhook_url not in err_text
