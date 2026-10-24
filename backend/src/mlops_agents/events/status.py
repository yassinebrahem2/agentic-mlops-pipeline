"""Status board and agent status derivation (I11)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.events.contract import (
    AgentState,
    AgentStatus,
    EventLog,
    EventRecord,
    PrRef,
    StatusBoard,
)
from mlops_agents.events.hub import row_to_event
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events as events_table

__all__ = [
    "SqliteStatusBoard",
    "derive_agent_status",
    "format_agent_status_line",
]

_SUMMARY_MAX_CHARS: Final[int] = 1500
_LAST_FINISHED_MAX_CHARS: Final[int] = 200


def derive_agent_status(
    definition: AgentDefinition,
    events: Sequence[EventRecord],
    *,
    task: TaskView | None = None,
    open_tasks: Sequence[TaskView] | None = None,
) -> AgentStatus:
    """Derive one agent's AgentStatus from its definition and project events.

    Derivation rules:
    - inactive for a definition with chat: false
    - running while its session has a running turn
    - waiting_approval while a turn waits
    - waiting_reply when, from message.sent alone, the last exchange the agent sent
      into has no message back to it yet
    - otherwise idle
    """
    sorted_events = sorted(events, key=lambda e: (e.seq, e.ts, e.id))

    # 1. State derivation
    if not definition.chat:
        state: AgentState = "inactive"
    else:
        turn_events = [
            e
            for e in sorted_events
            if e.source == definition.name
            and e.type in ("turn.started", "turn.waiting", "turn.ended")
        ]
        latest_turn = turn_events[-1] if turn_events else None

        if latest_turn is not None and latest_turn.type == "turn.started":
            state = "running"
        elif latest_turn is not None and latest_turn.type == "turn.waiting":
            state = "waiting_approval"
        else:
            message_events = [e for e in sorted_events if e.type == "message.sent"]

            def _sender(e: EventRecord) -> str:
                return str(
                    e.source
                    or e.payload.get("sender")
                    or e.payload.get("from_agent")
                    or e.payload.get("from")
                    or ""
                )

            def _recipient(e: EventRecord) -> str:
                return str(e.payload.get("recipient") or e.payload.get("to") or "")

            def _exchange(e: EventRecord) -> str | None:
                val = e.payload.get("exchange") or e.payload.get("exchange_id") or e.subject_id
                return str(val) if val is not None else None

            agent_sends: list[tuple[int, EventRecord]] = [
                (idx, e)
                for idx, e in enumerate(message_events)
                if _sender(e) == definition.name and _exchange(e) is not None
            ]

            if agent_sends:
                last_send_idx, last_send_event = agent_sends[-1]
                last_exchange = _exchange(last_send_event)
                has_reply = any(
                    _exchange(e) == last_exchange and _recipient(e) == definition.name
                    for e in message_events[last_send_idx + 1 :]
                )
                state = "idle" if has_reply else "waiting_reply"
            else:
                state = "idle"

    # 2. Last finished turn text (capped at 200 chars)
    turn_ended_events = [
        e for e in sorted_events if e.source == definition.name and e.type == "turn.ended"
    ]
    last_finished: str | None = None
    if turn_ended_events:
        final_text = turn_ended_events[-1].payload.get("final_text")
        if final_text is not None and isinstance(final_text, str):
            last_finished = final_text[:_LAST_FINISHED_MAX_CHARS]

    # 3. Open PRs from pr.opened events whose task is still in_review
    pr_events = [
        e
        for e in sorted_events
        if (e.source == definition.name or e.payload.get("agent") == definition.name)
        and e.type in ("pr.opened", "pr.updated")
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
            task_id = e.payload.get("task_id") or e.subject_id
            if task_id:
                pr_task_ids[num] = str(task_id)

    ended_task_ids = {
        str(e.payload.get("task_id") or e.subject_id)
        for e in sorted_events
        if e.type == "task.ended" and (e.payload.get("task_id") or e.subject_id)
    }

    open_prs: list[PrRef] = []
    for num, pr_ref in sorted(prs.items(), key=lambda item: item[0]):
        task_id = pr_task_ids.get(num)
        if task_id and task_id in ended_task_ids:
            continue
        if open_tasks is not None:
            matching = [t for t in open_tasks if t.id == task_id or t.pr_number == num]
            if matching and matching[0].status != "in_review":
                continue
        open_prs.append(pr_ref)

    # 4. Task view
    resolved_task = task
    if resolved_task is None and open_tasks is not None:
        for t in open_tasks:
            if getattr(t, "agent", None) == definition.name:
                resolved_task = t
                break

    return AgentStatus(
        agent=definition.name,
        display_name=definition.display_name,
        state=state,
        task=resolved_task,
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
    """Derives agent status and status summaries from the event log and catalog."""

    def __init__(
        self,
        catalog: AgentCatalog,
        events: EventLog,
        databases: Databases | None = None,
        tasks: Tasks | None = None,
    ) -> None:
        self._catalog = catalog
        self._events = events
        self._databases = databases
        self._tasks = tasks

    async def _fetch_events(self, project_id: str) -> list[EventRecord]:
        if self._databases is not None:
            engine = await self._databases.project(project_id)
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(select(events_table).order_by(events_table.c.seq.asc()))
                ).all()
                return [row_to_event(r) for r in rows]
        else:
            history_records = await self._events.history(
                project_id,
                types=[
                    "turn.started",
                    "turn.waiting",
                    "turn.ended",
                    "task.started",
                    "task.ended",
                    "pr.opened",
                    "pr.updated",
                    "message.sent",
                ],
                limit=1000,
            )
            return list(reversed(history_records))

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        project_events = await self._fetch_events(project_id)
        open_tasks = await self._tasks.open_tasks(project_id) if self._tasks is not None else None

        return [
            derive_agent_status(
                definition,
                project_events,
                open_tasks=open_tasks,
            )
            for definition in self._catalog.all()
        ]

    async def summary(self, project_id: str) -> str:
        statuses = await self.snapshot(project_id)
        text = "\n".join(format_agent_status_line(s) for s in statuses)
        if len(text) > _SUMMARY_MAX_CHARS:
            text = text[:_SUMMARY_MAX_CHARS]
        return text
