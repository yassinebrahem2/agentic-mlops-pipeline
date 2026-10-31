"""SignalsFromResults observer: turns fired results into signals (I22 -> I26)."""

from __future__ import annotations

from collections.abc import Sequence

from mlops_agents.monitoring.expectations.contract import Maintenance
from mlops_agents.monitoring.incidents.contract import Incidents, SignalDraft
from mlops_agents.monitoring.results.contract import (
    ResultsObserver,
    StoredResult,
    parse_source_id,
)


class SignalsFromResults(ResultsObserver):
    """Watches Results and raises fired_result signals for new fired records."""

    def __init__(self, incidents: Incidents, maintenance: Maintenance) -> None:
        self._incidents = incidents
        self._maintenance = maintenance

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        eligible = [r for r in results if r.fired and r.origin != "team" and not r.not_enough_data]
        # Oldest ran_at first
        eligible.sort(key=lambda r: (r.ran_at, r.id))

        for record in eligible:
            try:
                _kind, _ref, connection = parse_source_id(record.source_id)
                scope = connection if connection is not None else record.source_id
            except ValueError:
                scope = record.source_id

            if await self._maintenance.suspended(project_id, scope, record.ran_at):
                continue

            draft = SignalDraft(
                kind="fired_result",
                category=record.category,
                subject=record.subject,
                model=record.model,
                ran_at=record.ran_at,
                detail=record.detail,
                result_id=record.id,
                source_ref=record.source_id,
                expectation=None,
                cleared=False,
            )
            await self._incidents.raise_signal(project_id, draft, seen_at=record.seen_at)
