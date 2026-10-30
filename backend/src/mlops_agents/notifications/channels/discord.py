"""Discord notification channel implementation."""

from __future__ import annotations

from typing import Any, Literal

import httpx2

from mlops_agents.credentials.scrub import error_text, scrub_values
from mlops_agents.notifications.constants import (
    DISCORD_CONTENT_MAX,
    ERROR_MAX_CHARS,
    SEND_TIMEOUT_S,
)
from mlops_agents.notifications.contract import Channel, ChannelError, Message
from mlops_agents.notifications.render import render_message
from mlops_agents.notifications.settings import IntegrationSettings


class DiscordChannel(Channel):
    name: Literal["discord", "email"] = "discord"

    def __init__(self, settings: IntegrationSettings, http: httpx2.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def send(self, target: str, message: Message) -> None:
        webhook_url = await self._settings.webhook_url()
        secret_values = await self._settings.secret_values()

        if not webhook_url:
            msg = "Discord webhook URL is not configured."
            raise ChannelError(scrub_values(msg, secret_values)[:ERROR_MAX_CHARS])

        # Clean target: remove <@ and > if supplied
        target_id = target.strip().removeprefix("<@").removesuffix(">").strip()

        rendered = render_message(message)
        allowed_mentions: dict[str, list[str]]
        if message.urgent:
            content = f"<@{target_id}> {rendered}"
            allowed_mentions = {"parse": [], "users": [target_id]}
        else:
            content = rendered
            allowed_mentions = {"parse": []}

        content = content[:DISCORD_CONTENT_MAX]
        payload: dict[str, Any] = {
            "content": content,
            "allowed_mentions": allowed_mentions,
        }

        try:
            resp = await self._http.post(
                webhook_url,
                json=payload,
                timeout=SEND_TIMEOUT_S,
            )
            if resp.is_error:
                detail = resp.text
                err_msg = f"Discord webhook returned HTTP {resp.status_code}: {detail}"
                scrubbed = scrub_values(err_msg, secret_values)[:ERROR_MAX_CHARS]
                raise ChannelError(scrubbed)
        except ChannelError:
            raise
        except Exception as exc:
            scrubbed = scrub_values(error_text(exc), secret_values)[:ERROR_MAX_CHARS]
            raise ChannelError(scrubbed) from None
