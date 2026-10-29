"""The `data_summary` hook: every table of the project's connections, profiled through the
cache, within one size cap and one time budget."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Final

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.names import DATA_SUMMARY_HOOK
from mlops_agents.connections.contract import Connections, ConnectionView, TableRef
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.data import render
from mlops_agents.data.constants import (
    DATA_SUMMARY_MAX_CHARS,
    HOOK_BUDGET_S,
    MAX_TABLES,
    PROFILE_TIMEOUT_S,
)
from mlops_agents.data.profile import TableUnreadable
from mlops_agents.data.summaries import DataSummaries, SecretTable
from mlops_agents.errors import DomainError

logger = logging.getLogger(__name__)

# Room kept below the cap for the counted line and its line break, whatever its count.
COUNTED_LINE_RESERVE: Final = len(render.left_out_entries_line(10**9)) + 1


class DataSummaryHook:
    """Every line goes through the one cap, `DATA_SUMMARY_MAX_CHARS` (Decision #430). A table
    whose detail does not fit is listed by name and the next tables are still tried (Decision
    #431). Once a line would pass the cap, no further line is written or table profiled: the
    text ends with one line counting the entries left out, an entry being a connection or a
    table."""

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
        text.add(render.header(), entries=0)
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
        if not text.add(render.connection_line(view)):
            text.left_out(len(tables))
            return
        if not tables:
            text.add(render.no_tables_line(), entries=0)
        for ref in tables[:MAX_TABLES]:
            if text.stopped:  # counted, never profiled
                text.left_out(1)
            else:
                text.add(await self._table(project_id, view, ref, text))
        if rest := tables[MAX_TABLES:]:
            text.add(render.more_tables_line(rest), entries=len(rest))

    async def _table(
        self, project_id: str, view: ConnectionView, ref: TableRef, text: _Text
    ) -> str:
        listed = render.left_out_line(ref, render.SIZE_CAP)
        if not text.fits(listed):  # not even listed: the text stops here, unprofiled
            return listed
        remaining = text.deadline - time.monotonic()
        if remaining <= 0:
            return render.left_out_line(ref, render.TIME_BUDGET)
        try:
            data_version, profile = await self._summaries.versioned_profile(
                project_id, view.id, ref, min(PROFILE_TIMEOUT_S, remaining)
            )
        except SecretTable:
            return render.left_out_line(ref, render.DENYLISTED_FILE)
        except TableUnreadable as exc:
            # The team's host folder is no business of the prompt: the agent reads /data.
            mount = f"{DATA_MOUNT_ROOT}/{view.name}"
            return render.unreadable_line(
                ref, render.reason_without_folders(exc.message, exc.folders, mount)
            )
        except DomainError as exc:  # a file gone since the listing
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
        # A table that misses the cap is listed; the next ones are still tried (Decision #431).
        return detail if text.fits(detail) else listed


@dataclass
class _Text:
    """The hook's lines so far, against its size cap and time budget.

    `stopped`: a line no longer fits, so the rest is only counted, in the line `joined` ends
    with.
    """

    cap: int
    deadline: float
    stopped: bool = False
    _lines: list[str] = field(default_factory=list[str])
    _size: int = 0
    _left_out: int = 0

    def fits(self, block: str) -> bool:
        added = len(block) + (1 if self._lines else 0)
        return self._size + added <= self.cap - COUNTED_LINE_RESERVE

    def add(self, line: str, entries: int = 1) -> bool:
        """Writes `line`, or, when it does not fit, stops and counts its `entries`."""
        if not self.stopped and self.fits(line):
            self._size += len(line) + (1 if self._lines else 0)
            self._lines.append(line)
            return True
        self.stopped = True
        self.left_out(entries)
        return False

    def left_out(self, entries: int) -> None:
        self._left_out += entries

    def joined(self) -> str:
        counted = [render.left_out_entries_line(self._left_out)] if self._left_out else []
        return "\n".join([*self._lines, *counted])
