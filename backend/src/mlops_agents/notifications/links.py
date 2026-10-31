"""Builders of incident and inbox links in notification messages."""

from __future__ import annotations

from mlops_agents.notifications.constants import INBOX_PATH, INCIDENT_PATH


def incident_link(public_url: str | None, project_id: str, incident_id: str) -> str | None:
    """Builds a link to an incident if public_url is configured, else None."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{INCIDENT_PATH.format(project_id=project_id, incident_id=incident_id)}"


def inbox_link(public_url: str | None, project_id: str) -> str | None:
    """Builds a link to the project inbox if public_url is configured, else None."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{INBOX_PATH.format(project_id=project_id)}"
