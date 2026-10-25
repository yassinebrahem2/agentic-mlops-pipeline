from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.tasks.git import task_git
from mlops_agents.agents.tools.files import is_secret
from mlops_agents.storage.tables import file_claims, tasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner

MAX_DIFF_CHARS = 60_000


async def staged_diff_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    engine: AsyncEngine,
    task: TaskRef,
) -> str:
    """Returns the staged diff of what commit would record, capped at 60,000 chars.

    Never shows secret contents or files claimed by other tasks.
    Unstages everything before returning.
    """
    # 1. Stage everything
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["add", "-A"],
    )

    try:
        # 2. Inspect staged files
        staged_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["diff", "--cached", "--name-only", "-z"],
        )
        staged_paths = [p for p in staged_res.stdout.split("\0") if p]

        # 3. Exclude denylisted secrets and claim conflicts
        denylisted = [p for p in staged_paths if is_secret(p)]
        conflicts: list[str] = []
        if staged_paths:
            async with engine.connect() as conn:
                stmt = (
                    select(file_claims.c.path)
                    .join(tasks, tasks.c.id == file_claims.c.task_id)
                    .where(
                        tasks.c.status.in_(("active", "in_review")),
                        tasks.c.id != task.id,
                        file_claims.c.path.in_(staged_paths),
                    )
                )
                conflicts = [str(r) for r in (await conn.execute(stmt)).scalars().all()]

        to_unstage = list(dict.fromkeys(denylisted + conflicts))
        if to_unstage:
            await task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["reset", "-q", "--", *to_unstage],
            )

        # 4. Read base_sha
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(tasks.c.base_sha).where(tasks.c.id == task.id))
            ).scalar_one_or_none()
        base_sha = row or "HEAD"

        # 5. Diff against base
        diff_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["diff", "--cached", base_sha],
        )
        diff = diff_res.stdout
    finally:
        # 6. Unstage all
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
            check=False,
        )

    # 7. Cap output
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[diff cut]"
    return diff
