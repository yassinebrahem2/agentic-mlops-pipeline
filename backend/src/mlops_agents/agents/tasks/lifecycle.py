"""Task lifecycle operations: end_task and set_review_task (spec §6)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, insert, select, update

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.agents.tasks.worktrees import (
    clean_leftover_temp_files,
    create_worktree,
)
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.git import GitError
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.storage.tables import file_claims, task_reads, tasks
from mlops_agents.workspace.fs import rmtree

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.sandbox.contract import Sandbox
    from mlops_agents.storage.databases import Databases
    from mlops_agents.workspace.models import Project

logger = logging.getLogger(__name__)


async def start_task(
    *,
    runner: GitRunner,
    project: Project,
    repo_root: Path,
    worktree_path: Path,
    empty_hooks: Path,
    engine: AsyncEngine,
    events: EventLog,
    project_id: str,
    session_id: str,
    task_id: str,
    agent: str,
    title: str,
) -> TaskRef:
    branch, base_ref, base_sha = await create_worktree(
        runner=runner,
        project=project,
        repo_root=repo_root,
        worktree_path=worktree_path,
        empty_hooks=empty_hooks,
        task_id=task_id,
        agent=agent,
        title=title,
    )

    async with engine.begin() as conn:
        await conn.execute(
            insert(tasks).values(
                id=task_id,
                session_id=session_id,
                agent=agent,
                title=title,
                branch=branch,
                worktree=str(worktree_path),
                base_ref=base_ref,
                base_sha=base_sha,
                status="active",
                created_at=now_iso(),
            )
        )

    await events.record(
        project_id,
        source=agent,
        type="task.started",
        payload={"task_id": task_id, "title": title, "branch": branch},
        subject_id=task_id,
    )

    return TaskRef(
        id=task_id,
        title=title,
        branch=branch,
        worktree=worktree_path,
        status="active",
    )


async def resume_task(
    *,
    engine: AsyncEngine,
    session_id: str,
    agent: str,
    task_id: str,
) -> tuple[TaskRef, str | None]:
    async with engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()

    if row is None:
        raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

    if row["agent"] != agent:
        raise DomainError(
            "task_agent_mismatch",
            f"Task {task_id} belongs to agent {row['agent']!r}, not {agent!r}.",
            status=403,
        )

    if row["status"] not in ("active", "in_review"):
        raise DomainError(
            "task_not_open",
            f"Task {task_id} has status {row['status']!r} and cannot be resumed.",
            status=409,
        )

    old_session_id: str | None = None
    if row["session_id"] != session_id:
        old_session_id = str(row["session_id"])
        async with engine.begin() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == task_id).values(session_id=session_id)
            )

    worktree_path = Path(row["worktree"])
    await asyncio.to_thread(clean_leftover_temp_files, worktree_path)

    ref = TaskRef(
        id=row["id"],
        title=row["title"],
        branch=row["branch"],
        worktree=worktree_path,
        status=row["status"],
    )
    return ref, old_session_id


async def end_task(
    *,
    runner: GitRunner,
    repo_root: Path,
    empty_hooks: Path,
    databases: Databases,
    project_id: str,
    task_id: str,
    status: Literal["merged", "closed", "discarded"],
    sandbox: Sandbox | None = None,
    events: EventLog,
) -> None:
    """Ends a task:
    1. Removes container if sandbox is provided.
    2. Removes worktree.
    3. Deletes branch if status is 'merged' or 'discarded'.
    4. Releases all claims from file_claims.
    5. Removes reads from task_reads.
    6. Updates tasks table with status and ended_at.
    7. Emits task.ended event.
    """
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()

    if row is None:
        raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)
    if row["status"] not in ("active", "in_review"):
        # An ended task keeps its first outcome: no second cleanup, status change or event.
        raise DomainError(
            "task_not_open",
            f"Task {task_id} has status {row['status']!r} and has already ended.",
            status=409,
        )

    # 1. Container removal
    if sandbox is not None:
        with suppress(SandboxUnavailable):
            await sandbox.remove_task(project_id, task_id)

    # 2. Worktree removal
    worktree_path = Path(row["worktree"])
    try:
        await run_safe_git(
            runner,
            args=["worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
            empty_hooks=empty_hooks,
            check=False,
        )
    except GitError:
        # The folder is still deleted below; what git keeps is a stale entry `worktree prune` clears.
        logger.exception("Could not remove the worktree %s of task %s", worktree_path, task_id)
    await asyncio.to_thread(rmtree, worktree_path)

    # 3. Branch deletion on merged or discarded
    if status in ("merged", "discarded"):
        await run_safe_git(
            runner,
            args=["branch", "-D", str(row["branch"])],
            cwd=repo_root,
            empty_hooks=empty_hooks,
            check=False,
        )

    # 4 & 5 & 6. Database updates
    ended_at = now_iso()
    async with engine.begin() as conn:
        await conn.execute(delete(file_claims).where(file_claims.c.task_id == task_id))
        await conn.execute(delete(task_reads).where(task_reads.c.task_id == task_id))
        await conn.execute(
            update(tasks).where(tasks.c.id == task_id).values(status=status, ended_at=ended_at)
        )

    # 7. Emit task.ended event
    await events.record(
        project_id,
        source=str(row["agent"]),
        type="task.ended",
        payload={"task_id": task_id, "status": status},
        subject_id=task_id,
    )


async def set_review_task(
    *,
    engine: AsyncEngine,
    project_id: str,
    task_id: str,
    kind: Literal["github", "local"],
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> TaskView:
    del project_id
    async with engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(
                status="in_review",
                review_kind=kind,
                pr_number=pr_number,
                pr_url=pr_url,
            )
        )
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()

    if row is None:
        raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

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
