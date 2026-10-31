from __future__ import annotations

import json
from collections.abc import Iterator

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from mlops_agents.api.routers.integrations import DiscordUpdate, EmailUpdate
from mlops_agents.config import RunMode
from mlops_agents.notifications.contract import Message, OnCallPerson
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.proposals.payloads import Contact, OnCallHours
from mlops_agents.storage.tables import notification_log
from tests.api.conftest import LOCAL_URL
from tests.api.test_hosts import BAD_HOST
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
from tests.fakes.fake_smtp import FakeSmtpState, RunningFakeSmtp, start_fake_smtp


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


@pytest.mark.asyncio
async def test_integrations_routes_require_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/integrations/discord")
    assert res.status_code == 401
    res = await anonymous.put("/api/integrations/discord", json={"enabled": False})
    assert res.status_code == 401
    res = await anonymous.delete("/api/integrations/discord/webhook")
    assert res.status_code == 401

    res = await anonymous.get("/api/integrations/email")
    assert res.status_code == 401
    res = await anonymous.put("/api/integrations/email", json={"enabled": False, "host": ""})
    assert res.status_code == 401
    res = await anonymous.delete("/api/integrations/email/password")
    assert res.status_code == 401

    res = await anonymous.get("/api/integrations/public-url")
    assert res.status_code == 401
    res = await anonymous.put("/api/integrations/public-url", json={"public_url": None})
    assert res.status_code == 401

    res = await anonymous.post("/api/integrations/discord/test", json={"target": "123"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_discord_routes_roundtrip(client: httpx2.AsyncClient) -> None:
    # 1. Initial GET
    res = await client.get("/api/integrations/discord")
    assert res.status_code == 200
    assert res.json() == {"enabled": False, "webhook_set": False}

    # 2. Enabling without webhook gives 422
    res = await client.put("/api/integrations/discord", json={"enabled": True})
    assert res.status_code == 422

    # 3. Bad webhook URLs give 422
    for bad_url in [
        "not a url",
        "ftp://discord.com/webhook",
        "http://evil.com/webhook",  # non-loopback http forbidden
        "https://user:pass@discord.com/webhook",  # credentials forbidden
    ]:
        res = await client.put(
            "/api/integrations/discord",
            json={"enabled": False, "webhook_url": bad_url},
        )
        assert res.status_code == 422

    # 4. Set webhook on loopback http (fake)
    secret_url = "http://127.0.0.1:9999/webhook/secret-token-123"
    res = await client.put(
        "/api/integrations/discord",
        json={"enabled": False, "webhook_url": secret_url},
    )
    assert res.status_code == 200
    data = res.json()
    assert data == {"enabled": False, "webhook_set": True}
    assert "secret-token-123" not in res.text
    assert "webhook_url" not in data

    # 5. Enable with webhook set
    res = await client.put("/api/integrations/discord", json={"enabled": True})
    assert res.status_code == 200
    assert res.json() == {"enabled": True, "webhook_set": True}

    # 6. Delete webhook
    res = await client.delete("/api/integrations/discord/webhook")
    assert res.status_code == 204

    # 7. GET verifies webhook is unset
    res = await client.get("/api/integrations/discord")
    assert res.status_code == 200
    assert res.json()["webhook_set"] is False


def test_secrets_not_in_repr() -> None:
    d = DiscordUpdate(enabled=True, webhook_url="https://secret.token/url")
    assert "secret.token" not in repr(d)
    assert "webhook_url" not in repr(d)

    e = EmailUpdate(enabled=True, host="smtp.example.com", password="my-secret-password")
    assert "my-secret-password" not in repr(e)
    assert "password" not in repr(e)


@pytest.mark.asyncio
async def test_email_routes_roundtrip(client: httpx2.AsyncClient) -> None:
    # 1. Initial GET
    res = await client.get("/api/integrations/email")
    assert res.status_code == 200
    assert res.json() == {
        "enabled": False,
        "host": "",
        "port": 587,
        "user": "",
        "sender": "",
        "starttls": True,
        "password_set": False,
    }

    # 2. Enabling without host gives 422
    res = await client.put(
        "/api/integrations/email",
        json={
            "enabled": True,
            "host": "",
            "port": 587,
            "user": "u",
            "sender": "alerts@example.com",
            "starttls": True,
        },
    )
    assert res.status_code == 422

    # 3. Enabling without sender gives 422
    res = await client.put(
        "/api/integrations/email",
        json={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "user": "u",
            "sender": "",
            "starttls": True,
        },
    )
    assert res.status_code == 422

    # 4. Save settings and password
    res = await client.put(
        "/api/integrations/email",
        json={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "user": "mailer",
            "sender": "alerts@example.com",
            "starttls": True,
            "password": "secret-smtp-password",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data == {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 587,
        "user": "mailer",
        "sender": "alerts@example.com",
        "starttls": True,
        "password_set": True,
    }
    assert "secret-smtp-password" not in res.text
    assert "password" not in data

    # 5. Delete password
    res = await client.delete("/api/integrations/email/password")
    assert res.status_code == 204

    # 6. Verify password is unset
    res = await client.get("/api/integrations/email")
    assert res.status_code == 200
    assert res.json()["password_set"] is False


@pytest.mark.asyncio
async def test_public_url_routes_and_validation(client: httpx2.AsyncClient) -> None:
    # 1. Initial GET
    res = await client.get("/api/integrations/public-url")
    assert res.status_code == 200
    assert res.json() == {"public_url": None}

    # 2. Invalid public_urls give 422
    for bad_url in [
        "http://not-https.example.com",
        "https://example.com/has/a/path",
        "https://example.com?query=yes",
        "https://example.com#fragment",
        "https://user:pass@example.com",
        "not-a-url",
    ]:
        res = await client.put(
            "/api/integrations/public-url",
            json={"public_url": bad_url},
        )
        assert res.status_code == 422

    # 3. Valid public_url normalizes without trailing slash
    res = await client.put(
        "/api/integrations/public-url",
        json={"public_url": "https://monitor.company.org/"},
    )
    assert res.status_code == 200
    assert res.json() == {"public_url": "https://monitor.company.org"}

    res = await client.get("/api/integrations/public-url")
    assert res.status_code == 200
    assert res.json() == {"public_url": "https://monitor.company.org"}

    # 4. Clear public_url
    res = await client.put("/api/integrations/public-url", json={"public_url": None})
    assert res.status_code == 200
    assert res.json() == {"public_url": None}


@pytest.mark.asyncio
async def test_heartbeat_accepts_cached_public_url_host(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    custom_host = "public.example.org"
    public_url = f"https://{custom_host}"

    # Before setting public_url, heartbeat with custom_host is refused
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url=LOCAL_URL,
        headers={"Host": custom_host},
    ) as foreign_http:
        res = await foreign_http.get("/api/health/heartbeat")
        assert res.status_code == 400
        assert res.json() == BAD_HOST

    # Set public_url via PUT
    put_res = await client.put(
        "/api/integrations/public-url",
        json={"public_url": public_url},
    )
    assert put_res.status_code == 200

    # Now heartbeat with custom_host is accepted (even with foreign origin)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url=LOCAL_URL,
        headers={"Host": custom_host, "Origin": "https://foreign-monitor.org"},
    ) as foreign_http:
        hb_res = await foreign_http.get("/api/health/heartbeat")
        assert hb_res.status_code == 200
        assert hb_res.json() == {"ok": True}

        # Every other endpoint still refuses the custom host
        guarded_res = await foreign_http.get("/api/app")
        assert guarded_res.status_code == 400
        assert guarded_res.json() == BAD_HOST

        integrations_res = await foreign_http.get("/api/integrations/discord")
        assert integrations_res.status_code == 400
        assert integrations_res.json() == BAD_HOST


@pytest.mark.asyncio
async def test_channel_test_endpoint_discord(
    client: httpx2.AsyncClient, fake_discord: RunningFakeDiscord
) -> None:
    # 1. Set webhook to fake discord
    await client.put(
        "/api/integrations/discord",
        json={"enabled": True, "webhook_url": fake_discord.base_url},
    )

    # 2. Test success
    res = await client.post(
        "/api/integrations/discord/test",
        json={"target": "123456789"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "detail": None}
    assert len(fake_discord.state.posts) == 1

    # 3. Test failure (e.g. 500)
    fake_discord.state.fail_next(500, "Internal Server Error")
    res = await client.post(
        "/api/integrations/discord/test",
        json={"target": "123456789"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert "HTTP 500" in (data["detail"] or "")


@pytest.mark.asyncio
async def test_channel_test_endpoint_email(
    client: httpx2.AsyncClient, fake_smtp: RunningFakeSmtp
) -> None:
    # 1. Configure email to point to fake smtp
    await client.put(
        "/api/integrations/email",
        json={
            "enabled": True,
            "host": "127.0.0.1",
            "port": fake_smtp.port,
            "user": "",
            "sender": "sender@test.local",
            "starttls": False,
        },
    )

    # 2. Test success
    res = await client.post(
        "/api/integrations/email/test",
        json={"target": "dest@test.local"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "detail": None}
    assert len(fake_smtp.state.messages) == 1

    # 3. Test failure
    fake_smtp.state.fail_next_sends = 1
    res = await client.post(
        "/api/integrations/email/test",
        json={"target": "dest@test.local"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert data["detail"] is not None


@pytest.mark.asyncio
async def test_notifier_incident_link_and_run_mode(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    services = app.state.services

    # Configure public_url
    await client.put(
        "/api/integrations/public-url",
        json={"public_url": "https://dashboard.example.com"},
    )
    # Enable discord with dummy webhook so notify succeeds
    await client.put(
        "/api/integrations/discord",
        json={"enabled": True, "webhook_url": "http://127.0.0.1:1/dummy"},
    )

    person = OnCallPerson(
        id="bob",
        name="Bob",
        contacts=[Contact(channel="discord", address="999")],
        hours=OnCallHours(days=["mon"], start="09:00", end="17:00"),
        timezone="UTC",
        escalation_order=1,
    )

    # Create local notifier
    local_notifier = SqliteNotifier(
        databases=services.databases,
        settings=services.integration_settings,
        channels=services.notification_channels,
        events=services.events,
        clock=services.clock,
        run_mode=RunMode.LOCAL,
    )

    base_msg = Message(title="Crash", severity="critical")
    # Notify with incident_id
    await local_notifier.notify(
        project_id="prj_alpha",
        person=person,
        message=base_msg,
        kind="incident",
        incident_id="inc_42",
    )

    # Check notification log message payload
    engine = await services.databases.project("prj_alpha")

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(notification_log.c.message).where(notification_log.c.incident == "inc_42")
            )
        ).scalar_one()

    data = json.loads(str(row))
    assert data["link"] == "https://dashboard.example.com/projects/prj_alpha/incidents/inc_42"
    assert data["local_mode_note"] is True

    # Now test with Server mode
    server_notifier = SqliteNotifier(
        databases=services.databases,
        settings=services.integration_settings,
        channels=services.notification_channels,
        events=services.events,
        clock=services.clock,
        run_mode=RunMode.SERVER,
    )
    await server_notifier.notify(
        project_id="prj_alpha",
        person=person,
        message=base_msg,
        kind="incident",
        incident_id="inc_43",
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(notification_log.c.message).where(notification_log.c.incident == "inc_43")
            )
        ).scalar_one()
    data = json.loads(str(row))
    assert data["link"] == "https://dashboard.example.com/projects/prj_alpha/incidents/inc_43"
    assert data["local_mode_note"] is False

    # Now clear public_url and verify link is None
    await client.put("/api/integrations/public-url", json={"public_url": None})
    await local_notifier.notify(
        project_id="prj_alpha",
        person=person,
        message=base_msg,
        kind="incident",
        incident_id="inc_44",
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(notification_log.c.message).where(notification_log.c.incident == "inc_44")
            )
        ).scalar_one()
    data = json.loads(str(row))
    assert data["link"] is None


@pytest.mark.asyncio
async def test_channel_test_scrubs_secrets_on_non_channel_error(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    services = app.state.services
    secret_webhook = "http://127.0.0.1:9999/webhook/super-secret-token"
    await client.put(
        "/api/integrations/discord",
        json={"enabled": True, "webhook_url": secret_webhook},
    )

    class FailingChannel:
        name = "discord"

        async def send(self, target: str, message: Message) -> None:
            raise RuntimeError(f"Connection error to {secret_webhook}")

    services.notification_channels["discord"] = FailingChannel()

    res = await client.post(
        "/api/integrations/discord/test",
        json={"target": "123456789"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert data["detail"] is not None
    assert "super-secret-token" not in data["detail"]
    assert secret_webhook not in data["detail"]
    assert "***" in data["detail"]
