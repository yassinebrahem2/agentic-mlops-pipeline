"""Hooks query provider (I33)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.hooks.contract import HookView
from mlops_agents.monitoring.query import MonitoringQuery, QueryWhat
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks


class HooksQueryProvider:
    what: QueryWhat = "hooks"

    def __init__(self, hooks: Hooks) -> None:
        self._hooks = hooks

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[HookView]:
        all_hooks = await self._hooks.list(project_id, model=q.model)
        if q.id is not None:
            all_hooks = [h for h in all_hooks if h.id == q.id]
        if q.status is not None:
            all_hooks = [h for h in all_hooks if h.status == q.status]

        total = len(all_hooks)
        rows = all_hooks[q.offset : q.offset + q.limit]
        truncated = (q.offset + len(rows)) < total
        return Page(rows=rows, total=total, truncated=truncated)
