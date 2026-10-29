"""Scrubbing and event recording for tracking sync errors."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import update

from mlops_agents.credentials.scrub import error_text, scrub_url_secrets, scrub_values
from mlops_agents.events.log import EventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tracking_sources
from mlops_agents.tracking.constants import ERROR_MAX_CHARS

_log = logging.getLogger(__name__)


async def record_sync_error(
    databases: Databases,
    events: EventLog,
    github_token: Callable[[], Awaitable[str | None]],
    project_id: str,
    source_id: str,
    source_type: str,
    error_msg: str,
    credential: str | None,
) -> None:
    secrets_to_scrub = [credential] if credential else []
    try:
        gh_token = await github_token()
        if gh_token:
            secrets_to_scrub.append(gh_token)
    except Exception as exc:
        _log.warning("Could not resolve github token for error scrubbing: %s", error_text(exc))
    scrubbed = scrub_url_secrets(error_msg)
    scrubbed = scrub_values(scrubbed, secrets_to_scrub)
    if len(scrubbed) > ERROR_MAX_CHARS:
        scrubbed = scrubbed[:ERROR_MAX_CHARS]

    try:
        engine = await databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(tracking_sources)
                .where(tracking_sources.c.id == source_id)
                .values(last_error=scrubbed)
            )
    except Exception as exc:
        _log.warning("Could not update last_error for source %s: %s", source_id, error_text(exc))

    payload = {
        "source_id": source_id,
        "type": source_type,
        "ok": False,
        "versions": 0,
        "runs": 0,
        "metrics": 0,
        "ci_runs": 0,
        "evaluations": 0,
        "error": scrubbed,
    }
    try:
        await events.record(
            project_id,
            source="system",
            type="tracking.synced",
            subject_id=source_id,
            payload=payload,
        )
    except Exception as exc:
        _log.warning("Could not record tracking.synced error event: %s", error_text(exc))
