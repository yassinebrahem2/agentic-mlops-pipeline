from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.notifications.channels import default_channels
from mlops_agents.notifications.constants import RETRY_BACKOFFS_S
from mlops_agents.notifications.contract import Message, OnCallPerson
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.retries import RetryJob
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Contact, OnCallHours
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
from tests.fakes.fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    async def sleep(self, seconds: float) -> None:
        pass


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
def fake_smtp() -> Iterator[RunningFakeSmtp]:
    state = FakeSmtpState()
    server = start_fake_smtp(state)
    try:
        yield server
    finally:
        server.stop()


def make_person(
    id: str = "alice",
    contacts: list[Contact] | None = None,
) -> OnCallPerson:
    if contacts is None:
        contacts = [
            Contact(channel="discord", address="user_123"),
            Contact(channel="email", address="alice@example.com"),
        ]
    return OnCallPerson(
        id=id,
        name="Alice",
        contacts=contacts,
        hours=OnCallHours(days=["mon", "tue", "wed", "thu", "fri"], start="09:00", end="17:00"),
        timezone="UTC",
        escalation_order=1,
    )


async def test_notify_delivers_to_enabled_channels(
    tmp_path: Path, fake_discord: RunningFakeDiscord, fake_smtp: RunningFakeSmtp
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        await settings.set_discord(
            enabled=True,
            webhook_url=f"{fake_discord.base_url}/api/webhooks/123/token123",
        )
        await settings.set_email(
            enabled=True,
            host=fake_smtp.host,
            port=fake_smtp.port,
            user="test",
            sender="alerts@example.com",
            starttls=False,
        )

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)

            person = make_person()
            msg = Message(title="Test alert", severity="critical", urgent=True)

            res = await notifier.notify(
                "prj_1",
                person,
                msg,
                kind="incident",
                incident_id="inc_123",
            )

            assert res == "sent"
            assert len(fake_discord.state.posts) == 1
            assert len(fake_smtp.state.messages) == 1

            # Check notification_log rows
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()

            assert len(rows) == 2
            for r in rows:
                assert r["status"] == "sent"
                assert r["attempts"] == 1
                assert r["incident"] == "inc_123"
                assert r["escalation_step"] == 0

            # Check recorded events
            evts = await events.history("prj_1", types=["notification.sent"])
            assert len(evts) == 2
            for e in evts:
                assert e.subject_id == "inc_123"
                assert e.payload["person"] == "alice"
                assert e.payload["kind"] == "incident"
                assert e.payload["attempt"] == 1
                # Payload must NOT contain URLs, passwords or contact addresses
                payload_str = json.dumps(e.payload)
                assert "token123" not in payload_str
                assert "user_123" not in payload_str
                assert "alice@example.com" not in payload_str
    finally:
        await databases.dispose()


async def test_notify_disabled_channel_is_never_attempted(
    tmp_path: Path, fake_discord: RunningFakeDiscord, fake_smtp: RunningFakeSmtp
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        # Discord disabled, email enabled
        await settings.set_discord(enabled=False)
        await settings.set_email(
            enabled=True,
            host=fake_smtp.host,
            port=fake_smtp.port,
            user="test",
            sender="alerts@example.com",
            starttls=False,
        )

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)

            person = make_person()
            msg = Message(title="Alert", severity="low")
            res = await notifier.notify(
                "prj_1",
                person,
                msg,
                kind="incident",
                incident_id="inc_1",
            )

            assert res == "sent"
            # Discord was NEVER attempted
            assert len(fake_discord.state.posts) == 0
            assert len(fake_smtp.state.messages) == 1

            # In log: discord is failed, email is sent
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()

            disc_row = next(r for r in rows if r["channel"] == "discord")
            assert disc_row["status"] == "failed"
            assert disc_row["attempts"] == 0
            assert "disabled" in str(disc_row["last_error"]).lower()
    finally:
        await databases.dispose()


async def test_failure_then_retry_success_on_retry_job(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_secret"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)

        # Fail the first attempt
        fake_discord.state.fail_next(500)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            job = RetryJob(databases, notifier, clock)

            person = make_person(contacts=[Contact(channel="discord", address="u1")])
            msg = Message(title="Alert", severity="high")

            res = await notifier.notify("prj_1", person, msg, kind="incident", incident_id="inc_1")
            assert res == "retrying"

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            assert len(rows) == 1
            assert rows[0]["status"] == "retrying"
            assert rows[0]["attempts"] == 1
            # Next attempt is at clock + RETRY_BACKOFFS_S[0] (30s)
            expected_next = (clock.now() + timedelta(seconds=RETRY_BACKOFFS_S[0])).isoformat()
            assert rows[0]["next_attempt"] == expected_next

            # Advance clock by 10s (not yet due)
            clock.advance(timedelta(seconds=10))
            await job.run("prj_1", None)  # type: ignore

            # Still retrying, not attempted yet
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            assert rows[0]["status"] == "retrying"
            assert rows[0]["attempts"] == 1

            # Advance clock past 30s
            clock.advance(timedelta(seconds=25))
            await job.run("prj_1", None)  # type: ignore

            # Succeeded!
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            assert rows[0]["status"] == "sent"
            assert rows[0]["attempts"] == 2
            assert rows[0]["sent"] is not None

            # notification.sent recorded
            evts = await events.history("prj_1", types=["notification.sent"])
            assert len(evts) == 1
            assert evts[0].payload["attempt"] == 2
    finally:
        await databases.dispose()


async def test_failure_after_every_backoff_ends_failed(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_secret"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)

        # Fail all attempts: 1 inline + 3 retries = 4 failures
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)
        fake_discord.state.fail_next(500)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            job = RetryJob(databases, notifier, clock)

            person = make_person(contacts=[Contact(channel="discord", address="u1")])
            msg = Message(title="Alert", severity="high")

            res = await notifier.notify("prj_1", person, msg, kind="incident", incident_id="inc_1")
            assert res == "retrying"

            # Retry 1 (backoff 30s)
            clock.advance(timedelta(seconds=35))
            await job.run("prj_1", None)  # type: ignore

            # Retry 2 (backoff 120s)
            clock.advance(timedelta(seconds=125))
            await job.run("prj_1", None)  # type: ignore

            # Retry 3 (backoff 600s)
            clock.advance(timedelta(seconds=605))
            await job.run("prj_1", None)  # type: ignore

            # Exhausted! Row is now 'failed'
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            assert rows[0]["status"] == "failed"
            assert rows[0]["attempts"] == 4
            assert rows[0]["next_attempt"] is None

            # notification.failed event emitted
            evts = await events.history("prj_1", types=["notification.failed"])
            assert len(evts) == 1
            assert evts[0].payload["attempt"] == 4
    finally:
        await databases.dispose()


async def test_retrying_row_resumed_after_restart(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_secret"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)

        fake_discord.state.fail_next(500)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            person = make_person(contacts=[Contact(channel="discord", address="u1")])
            msg = Message(title="Alert", severity="high")
            await notifier.notify("prj_1", person, msg, kind="incident", incident_id="inc_1")
    finally:
        await databases.dispose()

    # RESTART APP: create fresh instances pointing to the same data folder
    new_databases = Databases(paths)
    try:
        new_settings = IntegrationSettings(new_databases, keystore)
        new_events = SqliteEventLog(new_databases)

        async with httpx2.AsyncClient() as http:
            new_channels = default_channels(new_settings, http)
            new_notifier = SqliteNotifier(
                new_databases, new_settings, new_channels, new_events, clock
            )
            new_job = RetryJob(new_databases, new_notifier, clock)

            # Advance clock and run retry job
            clock.advance(timedelta(seconds=35))
            await new_job.run("prj_1", None)  # type: ignore

            engine = await new_databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            assert rows[0]["status"] == "sent"
            assert rows[0]["attempts"] == 2
    finally:
        await new_databases.dispose()


async def test_failing_webhook_error_and_event_scrubs_secrets(
    tmp_path: Path, fake_discord: RunningFakeDiscord
) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        secret_token = "my_top_secret_discord_token"
        webhook_url = f"{fake_discord.base_url}/api/webhooks/999/{secret_token}"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)

        # Fail all attempts
        for _ in range(4):
            fake_discord.state.fail_next(500)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)
            job = RetryJob(databases, notifier, clock)

            person = make_person(contacts=[Contact(channel="discord", address="u1")])
            msg = Message(title="Alert", severity="high")
            await notifier.notify("prj_1", person, msg, kind="incident", incident_id="inc_1")

            for backoff in RETRY_BACKOFFS_S:
                clock.advance(timedelta(seconds=backoff + 5))
                await job.run("prj_1", None)  # type: ignore

            # Check log row
            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (await conn.execute(select(notification_log))).mappings().all()
            row = rows[0]
            assert secret_token not in str(row["last_error"])
            assert webhook_url not in str(row["last_error"])

            # Check notification.failed event
            evts = await events.history("prj_1", types=["notification.failed"])
            assert len(evts) == 1
            evt_str = json.dumps(evts[0].model_dump())
            assert secret_token not in evt_str
            assert webhook_url not in evt_str
    finally:
        await databases.dispose()


async def test_escalation_step_numbering(tmp_path: Path, fake_discord: RunningFakeDiscord) -> None:
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    try:
        keystore = MemoryKeyStore()
        settings = IntegrationSettings(databases, keystore)
        events = SqliteEventLog(databases)
        clock = FakeClock()

        webhook_url = f"{fake_discord.base_url}/api/webhooks/123/token_abc"
        await settings.set_discord(enabled=True, webhook_url=webhook_url)

        async with httpx2.AsyncClient() as http:
            channels = default_channels(settings, http)
            notifier = SqliteNotifier(databases, settings, channels, events, clock)

            alice = make_person(
                id="alice", contacts=[Contact(channel="discord", address="u_alice")]
            )
            bob = make_person(id="bob", contacts=[Contact(channel="discord", address="u_bob")])

            # Step 0: alice receives incident notification
            msg = Message(title="Critical", severity="critical")
            await notifier.notify("prj_1", alice, msg, kind="incident", incident_id="inc_10")

            # Step 1: bob receives escalation notification
            await notifier.notify("prj_1", bob, msg, kind="escalation", incident_id="inc_10")

            engine = await databases.project("prj_1")
            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(notification_log).order_by(notification_log.c.created)
                        )
                    )
                    .mappings()
                    .all()
                )

            assert len(rows) == 2
            assert rows[0]["person"] == "alice"
            assert rows[0]["kind"] == "incident"
            assert rows[0]["escalation_step"] == 0

            assert rows[1]["person"] == "bob"
            assert rows[1]["kind"] == "escalation"
            assert rows[1]["escalation_step"] == 1
    finally:
        await databases.dispose()
