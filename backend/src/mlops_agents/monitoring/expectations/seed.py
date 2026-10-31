"""Seeding expectations from HandoffPayload (Decision #448)."""

from __future__ import annotations

import logging

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.monitoring.expectations.store import SqliteExpectationsStore
from mlops_agents.proposals.payloads import DataFeedRef, HandoffPayload

logger = logging.getLogger(__name__)


class HandoffExpectationsSeeder:
    """Seeds feeds and checks from handoff payload and retires removed handoff keys."""

    def __init__(self, store: SqliteExpectationsStore) -> None:
        self._store = store

    async def seed_from_payload(self, project_id: str, payload: HandoffPayload) -> None:
        ops = payload.operations
        current_keys: set[tuple[str, str, str]] = set()

        # 1. Feeds
        for feed in ops.feeds:
            if isinstance(feed.ref, DataFeedRef):
                key = ExpectationKey(kind="feed", ref=feed.name, model=None)
                spec = ExpectationSpec(
                    key=key,
                    category="data_quality",
                    cadence=feed.cadence,
                    timezone=feed.timezone,
                    grace_minutes=feed.grace_minutes,
                    second_deadline=True,
                    source="handoff",
                )
            else:
                key = ExpectationKey(kind="job", ref=feed.name, model=None)
                spec = ExpectationSpec(
                    key=key,
                    category="operational",
                    cadence=feed.cadence,
                    timezone=feed.timezone,
                    grace_minutes=feed.grace_minutes,
                    second_deadline=True,
                    source="handoff",
                )

            current_keys.add((key.kind, key.ref, key.model or ""))
            await self._store.register(project_id, spec, active=True)

        # 2. Checks
        for check in ops.checks:
            key = ExpectationKey(kind="check", ref=check.check_id, model=None)
            spec = ExpectationSpec(
                key=key,
                category=check.category,
                cadence=check.cadence,
                timezone=check.timezone,
                grace_minutes=None,
                second_deadline=False,
                source="handoff",
            )
            current_keys.add((key.kind, key.ref, key.model or ""))
            await self._store.register(project_id, spec, active=True)

        # 3. Retire removed handoff keys
        retired = await self._store.retire_handoff_keys_not_in(project_id, current_keys)
        if retired:
            logger.info("Retired %d handoff expectations for project %s", len(retired), project_id)
