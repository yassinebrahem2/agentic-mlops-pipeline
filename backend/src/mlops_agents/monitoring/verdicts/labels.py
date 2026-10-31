"""Confirmation by labels after each sync (I31): label-based checks judge model incidents."""

from __future__ import annotations

from datetime import datetime

from mlops_agents.monitoring.verdicts.contract import Verdicts


class LabelConfirmation:
    """A SyncObserver: once a sync has stored new label results, they may decide a verdict."""

    def __init__(self, verdicts: Verdicts) -> None:
        self._verdicts = verdicts

    async def synced(self, project_id: str, now: datetime) -> None:
        await self._verdicts.confirm_by_labels(project_id)
