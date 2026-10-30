from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.notifications.channels.email import EmailChannel
from mlops_agents.notifications.contract import ChannelError, Message
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.fakes.fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp


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
def fake_smtp() -> Iterator[RunningFakeSmtp]:
    state = FakeSmtpState()
    server = start_fake_smtp(state)
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


async def test_email_delivers_rendered_message_not_urgent(
    fake_smtp: RunningFakeSmtp, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    await settings.set_email(
        enabled=True,
        host=fake_smtp.host,
        port=fake_smtp.port,
        user="test_user",
        sender="alerts@example.com",
        starttls=False,
    )

    channel = EmailChannel(settings)
    msg = Message(
        title="Check failed",
        severity="medium",
        cause="RMSE exceeds threshold",
        urgent=False,
    )
    await channel.send("dev@example.com", msg)

    assert len(fake_smtp.state.messages) == 1
    rec = fake_smtp.state.messages[0]
    assert rec.from_addr == "alerts@example.com"
    assert rec.to_addrs == ["dev@example.com"]
    assert rec.parsed_message["Subject"] == "Check failed"
    assert "Importance" not in rec.parsed_message
    assert "X-Priority" not in rec.parsed_message
    assert "RMSE exceeds threshold" in rec.raw_data


async def test_email_urgent_message_has_priority_headers(
    fake_smtp: RunningFakeSmtp, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    await settings.set_email(
        enabled=True,
        host=fake_smtp.host,
        port=fake_smtp.port,
        user="test_user",
        sender="alerts@example.com",
        starttls=False,
    )

    channel = EmailChannel(settings)
    msg = Message(
        title="Production incident",
        severity="critical",
        urgent=True,
    )
    await channel.send("oncall@example.com", msg)

    assert len(fake_smtp.state.messages) == 1
    rec = fake_smtp.state.messages[0]
    assert rec.parsed_message["Importance"] == "high"
    assert rec.parsed_message["X-Priority"] == "1"


async def test_email_subject_newlines_stripped_to_single_line(
    fake_smtp: RunningFakeSmtp, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    await settings.set_email(
        enabled=True,
        host=fake_smtp.host,
        port=fake_smtp.port,
        user="test_user",
        sender="alerts@example.com",
        starttls=False,
    )

    channel = EmailChannel(settings)
    msg = Message(
        title="Alert:\nHeader Injection\r\nAttempted",
        severity="low",
    )
    await channel.send("target@example.com", msg)

    rec = fake_smtp.state.messages[0]
    subject = rec.parsed_message["Subject"]
    assert "\n" not in subject
    assert "\r" not in subject
    assert "Alert: Header Injection Attempted" in subject


async def test_email_password_never_sent_over_plain_connection(
    fake_smtp: RunningFakeSmtp, integration_env: IntegrationSettings
) -> None:
    settings = integration_env
    # port != 465 and starttls is False, but password is set
    await settings.set_email(
        enabled=True,
        host=fake_smtp.host,
        port=fake_smtp.port,
        user="user",
        sender="alerts@example.com",
        starttls=False,
        password="my_secret_email_password",
    )

    channel = EmailChannel(settings)
    msg = Message(title="Incident", severity="critical")
    with pytest.raises(ChannelError) as exc_info:
        await channel.send("target@example.com", msg)

    err = str(exc_info.value).lower()
    assert "tls" in err or "starttls" in err
    assert len(fake_smtp.state.messages) == 0
    assert "my_secret_email_password" not in str(exc_info.value)
