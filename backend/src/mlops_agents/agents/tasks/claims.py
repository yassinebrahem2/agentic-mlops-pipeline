from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.tasks.contract import ClaimConflict
from mlops_agents.storage.tables import file_claims, tasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef


async def claim_paths(
    engine: AsyncEngine,
    task: TaskRef,
    paths: Sequence[str],
) -> None:
    """Claims paths for a task, raising ClaimConflict if claimed by another open task."""
    if not paths:
        return

    async with engine.begin() as conn:
        stmt = (
            select(file_claims.c.path, tasks.c.id, tasks.c.agent)
            .join(tasks, tasks.c.id == file_claims.c.task_id)
            .where(
                tasks.c.status.in_(("active", "in_review")),
                tasks.c.id != task.id,
                file_claims.c.path.in_(paths),
            )
        )
        conflicts = (await conn.execute(stmt)).mappings().all()
        if conflicts:
            conflict = conflicts[0]
            conflicting_path = conflict["path"]
            other_task_id = str(conflict["id"])
            other_agent = str(conflict["agent"])
            raise ClaimConflict(
                f"{conflicting_path} is claimed by agent {other_agent!r} in task {other_task_id}. "
                "Use SendMessage to coordinate.",
                agent=other_agent,
                task_id=other_task_id,
            )

        for p in paths:
            await conn.execute(
                sqlite_insert(file_claims).values(task_id=task.id, path=p).on_conflict_do_nothing()
            )
