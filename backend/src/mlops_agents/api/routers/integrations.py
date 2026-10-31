"""Integrations routes: Discord, Email, Test, and Public URL."""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.credentials.scrub import base_url_problem, error_text, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.notifications.constants import ERROR_MAX_CHARS
from mlops_agents.notifications.contract import (
    ChannelError,
    DiscordSettings,
    EmailSettings,
    Message,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    host_clean = host.strip("[]").lower()
    if host_clean in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


class DiscordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    webhook_url: str | None = Field(default=None, repr=False)


class EmailUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    sender: str = ""
    starttls: bool = True
    password: str | None = Field(default=None, repr=False)


class ChannelTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class ChannelTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str | None = None


class PublicUrlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_url: str | None = None


class PublicUrlView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_url: str | None


@router.get("/discord", response_model=DiscordSettings)
async def get_discord_settings(services: ServicesDep) -> DiscordSettings:
    """Returns the current Discord integration settings."""
    return await services.integration_settings.discord()


@router.put("/discord", response_model=DiscordSettings)
async def update_discord_settings(
    body: DiscordUpdate,
    services: ServicesDep,
) -> DiscordSettings:
    """Updates Discord integration settings."""
    if body.webhook_url is not None:
        problem = base_url_problem(body.webhook_url)
        if problem:
            raise DomainError(
                "invalid_webhook_url",
                problem,
                status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        parts = urlsplit(body.webhook_url)
        if parts.scheme != "https" and not (
            parts.scheme == "http" and _is_loopback(parts.hostname)
        ):
            raise DomainError(
                "invalid_webhook_url",
                "Discord webhook URL must use https (http is allowed only on loopback hosts).",
                status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    if body.enabled and body.webhook_url is None:
        current = await services.integration_settings.discord()
        if not current.webhook_set:
            raise DomainError(
                "channel_not_setup",
                "Discord webhook is not configured.",
                status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    return await services.integration_settings.set_discord(
        enabled=body.enabled,
        webhook_url=body.webhook_url,
    )


@router.delete("/discord/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def delete_discord_webhook(services: ServicesDep) -> None:
    """Clears the stored Discord webhook URL."""
    await services.integration_settings.clear_discord_webhook()


@router.get("/email", response_model=EmailSettings)
async def get_email_settings(services: ServicesDep) -> EmailSettings:
    """Returns the current Email integration settings."""
    return await services.integration_settings.email()


@router.put("/email", response_model=EmailSettings)
async def update_email_settings(
    body: EmailUpdate,
    services: ServicesDep,
) -> EmailSettings:
    """Updates Email integration settings."""
    if body.enabled and (not body.host.strip() or not body.sender.strip()):
        raise DomainError(
            "channel_not_setup",
            "Email host and sender are required when email is enabled.",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    return await services.integration_settings.set_email(
        enabled=body.enabled,
        host=body.host,
        port=body.port,
        user=body.user,
        sender=body.sender,
        starttls=body.starttls,
        password=body.password,
    )


@router.delete("/email/password", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_password(services: ServicesDep) -> None:
    """Clears the stored Email SMTP password."""
    await services.integration_settings.clear_email_password()


@router.post("/{channel}/test", response_model=ChannelTest)
async def test_channel(
    channel: Literal["discord", "email"],
    body: ChannelTestRequest,
    services: ServicesDep,
) -> ChannelTest:
    """Sends a single test message directly through the specified channel."""
    ch = services.notification_channels.get(channel)
    if ch is None:
        return ChannelTest(ok=False, detail=f"Channel {channel} is not available")

    test_message = Message(
        title="Test notification",
        severity="low",
        lines=["This is a test notification from MLOps Agents."],
    )

    try:
        await ch.send(body.target, test_message)
        return ChannelTest(ok=True, detail=None)
    except ChannelError as exc:
        return ChannelTest(ok=False, detail=str(exc))
    except Exception as exc:
        secrets = await services.integration_settings.secret_values()
        scrubbed = scrub_values(error_text(exc), secrets)[:ERROR_MAX_CHARS]
        return ChannelTest(ok=False, detail=scrubbed)


@router.get("/public-url", response_model=PublicUrlView)
async def get_public_url(services: ServicesDep) -> PublicUrlView:
    """Returns the configured public URL."""
    return PublicUrlView(public_url=await services.integration_settings.public_url())


@router.put("/public-url", response_model=PublicUrlView)
async def update_public_url(
    body: PublicUrlUpdate,
    services: ServicesDep,
) -> PublicUrlView:
    """Sets or clears the app's public URL."""
    if body.public_url is None:
        await services.integration_settings.set_public_url(None)
        return PublicUrlView(public_url=None)

    url = body.public_url.strip()
    problem = base_url_problem(url)
    if problem:
        raise DomainError(
            "invalid_public_url",
            problem,
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DomainError(
            "invalid_public_url",
            "public_url must start with https://",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if parts.path not in ("", "/"):
        raise DomainError(
            "invalid_public_url",
            "public_url must not contain a path",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if parts.query:
        raise DomainError(
            "invalid_public_url",
            "public_url must not contain query parameters",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if parts.fragment:
        raise DomainError(
            "invalid_public_url",
            "public_url must not contain a fragment",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if parts.username or parts.password:
        raise DomainError(
            "invalid_public_url",
            "public_url must not contain credentials",
            status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    normalized = f"https://{parts.netloc}".rstrip("/")
    await services.integration_settings.set_public_url(normalized)
    return PublicUrlView(public_url=normalized)
