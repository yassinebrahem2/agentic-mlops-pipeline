"""Notification channels and defaults."""

from __future__ import annotations

import httpx2

from mlops_agents.notifications.channels.discord import DiscordChannel
from mlops_agents.notifications.channels.email import EmailChannel
from mlops_agents.notifications.contract import Channel
from mlops_agents.notifications.settings import IntegrationSettings


def default_channels(settings: IntegrationSettings, http: httpx2.AsyncClient) -> dict[str, Channel]:
    """Returns the standard notification channels dictionary."""
    return {
        "discord": DiscordChannel(settings, http),
        "email": EmailChannel(settings),
    }
