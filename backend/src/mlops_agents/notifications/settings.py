"""Integration settings (Discord, email, public_url) and their secrets."""

from __future__ import annotations

import asyncio
import contextlib
import json
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.credentials.keystore import KeyStore, KeyStoreError
from mlops_agents.notifications.contract import (
    DISCORD_SETTINGS_KEY,
    DISCORD_WEBHOOK_REF,
    EMAIL_PASSWORD_REF,
    EMAIL_SETTINGS_KEY,
    PUBLIC_URL_KEY,
    DiscordSettings,
    EmailSettings,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import app_settings


class IntegrationSettings:
    """The one reader and writer of Discord, Email and Public URL integration settings."""

    def __init__(self, databases: Databases, keystore: KeyStore) -> None:
        self._databases = databases
        self._keystore = keystore
        self._cached_public_url_host: str | None = None

    @property
    def cached_public_url_host(self) -> str | None:
        return self._cached_public_url_host

    async def init_cache(self) -> None:
        """Loads cached settings into memory (e.g. at startup)."""
        url = await self.public_url()
        self._update_cached_host(url)

    def _update_cached_host(self, url: str | None) -> None:
        if not url:
            self._cached_public_url_host = None
            return
        try:
            parts = urlsplit(url)
            self._cached_public_url_host = parts.hostname
        except Exception:
            self._cached_public_url_host = None

    async def webhook_url(self) -> str | None:
        """The Discord webhook URL from KeyStore."""
        try:
            return await asyncio.to_thread(self._keystore.get, DISCORD_WEBHOOK_REF)
        except KeyStoreError:
            return None

    async def email_password(self) -> str | None:
        """The SMTP email password from KeyStore."""
        try:
            return await asyncio.to_thread(self._keystore.get, EMAIL_PASSWORD_REF)
        except KeyStoreError:
            return None

    async def discord(self) -> DiscordSettings:
        webhook = await self.webhook_url()
        engine = await self._databases.app()
        async with engine.connect() as conn:
            raw = await conn.scalar(
                select(app_settings.c.value).where(app_settings.c.key == DISCORD_SETTINGS_KEY)
            )

        enabled = False
        if raw:
            try:
                data = json.loads(str(raw))
                enabled = bool(data.get("enabled", False))
            except Exception:
                enabled = False

        return DiscordSettings(
            enabled=enabled,
            webhook_set=bool(webhook),
        )

    async def set_discord(
        self,
        enabled: bool,
        webhook_url: str | None = None,
    ) -> DiscordSettings:
        if webhook_url is not None:
            await asyncio.to_thread(self._keystore.set, DISCORD_WEBHOOK_REF, webhook_url)

        val_json = json.dumps({"enabled": enabled})
        engine = await self._databases.app()
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(app_settings)
                .values(key=DISCORD_SETTINGS_KEY, value=val_json)
                .on_conflict_do_update(
                    index_elements=[app_settings.c.key],
                    set_={"value": val_json},
                )
            )
            await conn.execute(stmt)

        return await self.discord()

    async def clear_discord_webhook(self) -> None:
        with contextlib.suppress(KeyStoreError):
            await asyncio.to_thread(self._keystore.delete, DISCORD_WEBHOOK_REF)

    async def email(self) -> EmailSettings:
        password = await self.email_password()
        engine = await self._databases.app()
        async with engine.connect() as conn:
            raw = await conn.scalar(
                select(app_settings.c.value).where(app_settings.c.key == EMAIL_SETTINGS_KEY)
            )

        data: dict[str, object] = {}
        if raw:
            try:
                data = json.loads(str(raw))
            except Exception:
                data = {}

        return EmailSettings(
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "")),
            port=int(str(data.get("port", 587))),
            user=str(data.get("user", "")),
            sender=str(data.get("sender", "")),
            starttls=bool(data.get("starttls", True)),
            password_set=bool(password),
        )

    async def set_email(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        user: str,
        sender: str,
        starttls: bool,
        password: str | None = None,
    ) -> EmailSettings:
        if password is not None:
            await asyncio.to_thread(self._keystore.set, EMAIL_PASSWORD_REF, password)

        val_json = json.dumps(
            {
                "enabled": enabled,
                "host": host,
                "port": port,
                "user": user,
                "sender": sender,
                "starttls": starttls,
            }
        )
        engine = await self._databases.app()
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(app_settings)
                .values(key=EMAIL_SETTINGS_KEY, value=val_json)
                .on_conflict_do_update(
                    index_elements=[app_settings.c.key],
                    set_={"value": val_json},
                )
            )
            await conn.execute(stmt)

        return await self.email()

    async def clear_email_password(self) -> None:
        with contextlib.suppress(KeyStoreError):
            await asyncio.to_thread(self._keystore.delete, EMAIL_PASSWORD_REF)

    async def public_url(self) -> str | None:
        engine = await self._databases.app()
        async with engine.connect() as conn:
            raw = await conn.scalar(
                select(app_settings.c.value).where(app_settings.c.key == PUBLIC_URL_KEY)
            )
        val = str(raw) if raw is not None else None
        self._update_cached_host(val)
        return val

    async def set_public_url(self, url: str | None) -> None:
        engine = await self._databases.app()
        async with engine.begin() as conn:
            if url is None:
                await conn.execute(delete(app_settings).where(app_settings.c.key == PUBLIC_URL_KEY))
            else:
                stmt = (
                    sqlite_insert(app_settings)
                    .values(key=PUBLIC_URL_KEY, value=url)
                    .on_conflict_do_update(
                        index_elements=[app_settings.c.key],
                        set_={"value": url},
                    )
                )
                await conn.execute(stmt)
        self._update_cached_host(url)

    async def secret_values(self) -> list[str]:
        """Webhook URL, its token segment, and the email password."""
        secrets: list[str] = []
        webhook = await self.webhook_url()
        if webhook:
            secrets.append(webhook)
            with contextlib.suppress(Exception):
                parts = urlsplit(webhook)
                token_segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
                if token_segment and token_segment != parts.path:
                    secrets.append(token_segment)

        password = await self.email_password()
        if password:
            secrets.append(password)

        return [s for s in secrets if s]
