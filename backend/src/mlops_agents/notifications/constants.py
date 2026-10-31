"""Notifications constants (one source for notifications plan)."""

from typing import Final

SEND_TIMEOUT_S: Final[int] = 10
RETRY_BACKOFFS_S: Final[tuple[int, ...]] = (30, 120, 600)
FAILURE_SIGNAL_AFTER: Final[int] = 3
DISCORD_CONTENT_MAX: Final[int] = 2000
ERROR_MAX_CHARS: Final[int] = 500
INCIDENT_PATH: Final[str] = "/projects/{project_id}/incidents/{incident_id}"
INBOX_PATH: Final[str] = "/projects/{project_id}/inbox"
ON_CALL_SEEDED_KEY: Final[str] = "on_call_handoff_version"
