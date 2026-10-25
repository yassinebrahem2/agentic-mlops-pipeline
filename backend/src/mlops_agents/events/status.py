"""Status board and agent status derivation (I11)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.contract import AgentSessions, TaskView
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.events.contract import (
    AgentState,
    AgentStatus,
    EventLog,
    EventRecord,
    PrRef,
    StatusBoard,
)

__all__ = [
    "SqliteStatusBoard",
    "derive_agent_status",
    "format_agent_status_line",
]

_SUMMARY_MAX_CHARS: Final[int] = 1500
_HISTORY_PER_TYPE_LIMIT: Final[int] = 100


def derive_agent_status(
    definition: AgentDefinition,
    events: Sequence[EventRecord],
    *,
    turn_state: Literal["running", "waiting"] | None = None,
    open_tasks: Sequence[TaskView] | None = None,
) -> AgentStatus:
    """Derive one agent's AgentStatus from its definition, events, and turn states.

    Derivation rules:
    - inactive for a definition with chat: false
    - running while its session has a running turn (from turn_state)
    - waiting_approval while a turn waits (from turn_state)
    - waiting_reply when, from message.sent alone, the last exchange the agent sent
      into has no message back to it yet
    - otherwise idle
    """
    sorted_events = sorted(events, key=lambda e: (e.seq, e.ts, e.id))

    # 1. State derivation
    if not definition.chat:
        state: AgentState = "inactive"
    elif turn_state == "running":
        state = "running"
    elif turn_state == "waiting":
        state = "waiting_approval"
    else:
        # Check message.sent alone: read only fixed keys (to, exchange_id, hop) and source as sender
        message_events = [e for e in sorted_events if e.type == "message.sent"]
        agent_sends: list[tuple[int, EventRecord]] = [
            (idx, e)
            for idx, e in enumerate(message_events)
            if e.source == definition.name and bool(e.payload.get("exchange_id"))
        ]

        if agent_sends:
            last_send_idx, last_send_event = agent_sends[-1]
            last_exchange = str(last_send_event.payload.get("exchange_id") or "")
            has_reply = any(
                str(e.payload.get("exchange_id") or "") == last_exchange
                and str(e.payload.get("to") or "") == definition.name
                for e in message_events[last_send_idx + 1 :]
            )
            state = "idle" if has_reply else "waiting_reply"
        else:
            state = "idle"

    # 2. Last finished turn text (already capped at 200 chars by observer)
    turn_ended_events = [
        e for e in sorted_events if e.source == definition.name and e.type == "turn.ended"
    ]
    last_finished: str | None = None
    if turn_ended_events:
        final_text = turn_ended_events[-1].payload.get("final_text")
        if isinstance(final_text, str):
            last_finished = final_text

    # 3. Open PRs from pr.opened/pr.updated whose task is in open_tasks with status in_review
    open_prs: list[PrRef] = []
    if open_tasks is not None:
        in_review_task_ids = {t.id for t in open_tasks if t.status == "in_review"}
        pr_events = [
            e
            for e in sorted_events
            if e.source == definition.name and e.type in ("pr.opened", "pr.updated")
        ]
        prs: dict[int, PrRef] = {}
        pr_task_ids: dict[int, str] = {}
        for e in pr_events:
            num = e.payload.get("number")
            if isinstance(num, int):
                existing = prs.get(num)
                url = str(e.payload.get("url") or (existing.url if existing else ""))
                title = str(e.payload.get("title") or (existing.title if existing else ""))
                prs[num] = PrRef(number=num, url=url, title=title)
                task_id = e.payload.get("task_id")
                if task_id:
                    pr_task_ids[num] = str(task_id)

        for num, pr_ref in sorted(prs.items(), key=lambda item: item[0]):
            task_id = pr_task_ids.get(num)
            if task_id and task_id in in_review_task_ids:
                open_prs.append(pr_ref)

    # 4. Task view: find open task for this agent
    current_task: TaskView | None = None
    if open_tasks is not None:
        for t in open_tasks:
            if t.agent == definition.name:
                current_task = t
                break

    return AgentStatus(
        agent=definition.name,
        display_name=definition.display_name,
        state=state,
        task=current_task,
        open_prs=open_prs,
        last_finished=last_finished,
    )


def format_agent_status_line(status: AgentStatus) -> str:
    """Format one agent's status into a single summary line."""
    parts = [f"- {status.display_name}: {status.state}"]
    if status.task is not None:
        parts.append(f"task: {status.task.title}")
    if status.open_prs:
        pr_text = ", ".join(f"#{pr.number}" for pr in status.open_prs)
        parts.append(f"PRs: {pr_text}")
    return ", ".join(parts)


class SqliteStatusBoard(StatusBoard):
    """Derives agent status and status summaries from the event log, sessions, and catalog."""

    def __init__(
        self,
        catalog: AgentCatalog,
        events: EventLog,
        sessions: AgentSessions | None = None,
        tasks: Tasks | None = None,
    ) -> None:
        self._catalog = catalog
        self._events = events
        self._sessions = sessions
        self._tasks = tasks

    def bind_sessions(self, sessions: AgentSessions) -> None:
        """The session manager binds itself once; a second call raises RuntimeError."""
        if self._sessions is not None:
            raise RuntimeError("sessions already bound to status board")
        self._sessions = sessions

    async def _fetch_events(self, project_id: str) -> list[EventRecord]:
        # Query each type with its own bound so high-volume message.sent cannot push pr.* out.
        turn_ended = await self._events.history(
            project_id, types=["turn.ended"], limit=_HISTORY_PER_TYPE_LIMIT
        )
        pr_opened = await self._events.history(
            project_id, types=["pr.opened"], limit=_HISTORY_PER_TYPE_LIMIT
        )
        pr_updated = await self._events.history(
            project_id, types=["pr.updated"], limit=_HISTORY_PER_TYPE_LIMIT
        )
        message_sent = await self._events.history(
            project_id, types=["message.sent"], limit=_HISTORY_PER_TYPE_LIMIT
        )
        return sorted([*turn_ended, *pr_opened, *pr_updated, *message_sent], key=lambda e: e.seq)

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        project_events = await self._fetch_events(project_id)
        open_tasks = await self._tasks.open_tasks(project_id) if self._tasks is not None else None
        turn_states = (
            await self._sessions.turn_states(project_id) if self._sessions is not None else {}
        )

        return [
            derive_agent_status(
                definition,
                project_events,
                turn_state=turn_states.get(definition.name),
                open_tasks=open_tasks,
            )
            for definition in self._catalog.all()
        ]

    async def summary(self, project_id: str) -> str:
        statuses = await self.snapshot(project_id)
        lines: list[str] = []
        total_len = 0
        for s in statuses:
            line = format_agent_status_line(s)
            added_len = len(line) + (1 if lines else 0)
            if total_len + added_len > _SUMMARY_MAX_CHARS:
                break
            lines.append(line)
            total_len += added_len
        return "\n".join(lines)
