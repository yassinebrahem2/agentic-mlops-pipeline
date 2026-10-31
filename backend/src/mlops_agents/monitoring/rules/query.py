"""Rules query provider (I33)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.query import MonitoringQuery, QueryWhat
from mlops_agents.monitoring.rules.contract import RuleView
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    from mlops_agents.monitoring.rules.contract import Rules


class RulesQueryProvider:
    what: QueryWhat = "rules"

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[RuleView]:
        all_rules = await self._rules.list(project_id, model=q.model)
        if q.id is not None:
            all_rules = [r for r in all_rules if r.id == q.id]

        total = len(all_rules)
        rows = all_rules[q.offset : q.offset + q.limit]
        truncated = (q.offset + len(rows)) < total
        return Page(rows=rows, total=total, truncated=truncated)
