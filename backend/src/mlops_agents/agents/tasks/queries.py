"""The read side of tasks: a task's view, the open tasks and a session's current task."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.engine import RowMapping

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import tasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["current", "open_tasks", "task_ref", "task_view", "view"]


def task_ref(
    project_id: str, row: RowMapping, pending_notes: Mapping[tuple[str, str], str]
) -> TaskRef:
    # A pure read: the session view comes through here too, so the worktree is readied
    # (and recreated when gone) only in `TasksService.files`, on the turn path.
    return TaskRef(
        id=row["id"],
        title=row["title"],
        branch=row["branch"],
        worktree=Path(row["worktree"]),
        status=row["status"],
        pending_note=pending_notes.get((project_id, row["id"])),
    )


def task_view(row: RowMapping) -> TaskView:
    return TaskView(
        id=row["id"],
        agent=row["agent"],
        title=row["title"],
        branch=row["branch"],
        status=row["status"],
        review_kind=row["review_kind"],
        pr_number=row["pr_number"],
        pr_url=row["pr_url"],
    )


async def current(
    engine: AsyncEngine,
    project_id: str,
    session_id: str,
    current_tasks: dict[tuple[str, str], str],
    pending_notes: Mapping[tuple[str, str], str],
) -> TaskRef | None:
    """The session's open task: the cached one while it is still open and the session's, else
    the newest open task of the session, which then becomes the cached one."""
    task_id = current_tasks.get((project_id, session_id))
    if task_id:
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            )
        if (
            row is not None
            and row["status"] in ("active", "in_review")
            and row["session_id"] == session_id
        ):
            return task_ref(project_id, row, pending_notes)
        current_tasks.pop((project_id, session_id), None)

    async with engine.connect() as conn:
        stmt = (
            select(tasks)
            .where(
                tasks.c.session_id == session_id,
                tasks.c.status.in_(("active", "in_review")),
            )
            .order_by(tasks.c.created_at.desc())
        )
        row = (await conn.execute(stmt)).mappings().first()

    if row is None:
        return None

    current_tasks[(project_id, session_id)] = row["id"]
    return task_ref(project_id, row, pending_notes)


async def view(engine: AsyncEngine, task_id: str) -> TaskView:
    async with engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()

    if row is None:
        raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

    return task_view(row)


async def open_tasks(engine: AsyncEngine) -> list[TaskView]:
    async with engine.connect() as conn:
        stmt = (
            select(tasks)
            .where(tasks.c.status.in_(("active", "in_review")))
            .order_by(tasks.c.created_at.asc())
        )
        rows = (await conn.execute(stmt)).mappings().all()
    return [task_view(row) for row in rows]
