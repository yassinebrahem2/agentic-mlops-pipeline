"""The `data_summary` hook: every table of the project's connections, profiled through the
cache, within one size cap and one time budget."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.names import DATA_SUMMARY_HOOK
from mlops_agents.connections.contract import Connections, ConnectionView, TableRef
from mlops_agents.data import render
from mlops_agents.data.constants import (
    DATA_SUMMARY_MAX_CHARS,
    HOOK_BUDGET_S,
    MAX_TABLES,
    PROFILE_TIMEOUT_S,
)
from mlops_agents.data.summaries import DataSummaries
from mlops_agents.errors import DomainError

logger = logging.getLogger(__name__)


class DataSummaryHook:
    name: str = DATA_SUMMARY_HOOK

    def __init__(self, connections: Connections, summaries: DataSummaries) -> None:
        self._connections = connections
        self._summaries = summaries

    async def render(self, ctx: HookContext) -> str:
        """`""` without connections. A connection or table that fails is one line; anything
        else a bug, which the registry reports as the hook's one line."""
        text = _Text(cap=DATA_SUMMARY_MAX_CHARS, deadline=time.monotonic() + HOOK_BUDGET_S)
        views = await self._connections.list(ctx.project_id)
        if not views:
            return ""
        text.add(render.header())
        for view in views:
            await self._connection(ctx.project_id, view, text)
        return text.joined()

    async def _connection(self, project_id: str, view: ConnectionView, text: _Text) -> None:
        if view.status == "unreachable":
            text.add(render.unreachable_line(view, view.status_detail))
            return
        try:
            tables = await self._connections.tables(project_id, view.id)
        except DomainError as exc:  # the folder went away since `list` read it
            text.add(render.unreachable_line(view, exc.message))
            return
        text.add(render.connection_line(view))
        if not tables:
            text.add(render.no_tables_line())
        for ref in tables[:MAX_TABLES]:
            text.add(await self._table(project_id, view.id, ref, text))
        if rest := tables[MAX_TABLES:]:
            text.add(render.more_tables_line(rest))

    async def _table(self, project_id: str, connection_id: str, ref: TableRef, text: _Text) -> str:
        if text.full:
            return render.left_out_line(ref, render.SIZE_CAP)
        remaining = text.deadline - time.monotonic()
        if remaining <= 0:
            return render.left_out_line(ref, render.TIME_BUDGET)
        try:
            data_version, profile = await self._summaries.versioned_profile(
                project_id, connection_id, ref, min(PROFILE_TIMEOUT_S, remaining)
            )
        except DomainError as exc:  # unreadable, or a file gone since the listing
            return render.unreadable_line(ref, exc.message)
        except TimeoutError:  # waited on another render's profile of this table
            return render.left_out_line(ref, render.TIME_BUDGET)
        except OSError:  # after TimeoutError, its subclass
            # The cache file could not be read or written (held open, disk full). Its text
            # names the app data folder, so the line gives a fixed reason and the log the rest.
            logger.warning("profile cache unavailable for a table", exc_info=True)
            return render.left_out_line(ref, render.CACHE_UNAVAILABLE)
        if profile.timed_out and profile.timeout_s < PROFILE_TIMEOUT_S:
            # Cut by the budget, not the full timeout: the next turn profiles it again.
            return render.left_out_line(ref, render.TIME_BUDGET)
        detail = render.render_versioned_table(ref, data_version, profile)
        if not text.fits(detail):
            text.full = True
            return render.left_out_line(ref, render.SIZE_CAP)
        return detail


@dataclass
class _Text:
    """The hook's lines so far, against its size cap and time budget."""

    cap: int
    deadline: float
    full: bool = False
    _lines: list[str] = field(default_factory=list[str])
    _size: int = 0

    def fits(self, detail: str) -> bool:
        return self._size + 1 + len(detail) <= self.cap

    def add(self, line: str) -> None:
        self._size += len(line) + (1 if self._lines else 0)
        self._lines.append(line)

    def joined(self) -> str:
        return "\n".join(self._lines)
