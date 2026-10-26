from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.constants import MAX_DIFF_CHARS, cap_diff
from mlops_agents.agents.tasks.git import task_git
from mlops_agents.agents.temp_files import is_generated_temp_file
from mlops_agents.agents.tools.files import is_secret
from mlops_agents.storage.tables import file_claims, tasks

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner

__all__ = [
    "MAX_DIFF_CHARS",
    "get_candidate_paths",
    "staged_diff_task",
]


async def get_candidate_paths(
    runner: GitRunner,
    *,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
) -> list[str]:
    """Returns all changed, deleted, and untracked file paths relative to worktree.

    Excludes ignored files. Does not run git add, so untracked secrets never enter
    the git object database.
    """
    res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["status", "--porcelain=v1", "-uall", "-z"],
    )
    tokens = res.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        # Format is 'XY path'
        status = token[:2]
        path = token[3:]
        paths.append(path)
        # If it's a rename, the next token is the original path
        if "R" in status:
            i += 1
            if i < len(tokens) and tokens[i]:
                paths.append(tokens[i])
        i += 1
    return [p for p in paths if not is_generated_temp_file(p)]


async def staged_diff_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    engine: AsyncEngine,
    task: TaskRef,
) -> str:
    """Returns the staged diff of what commit would record, capped at 30,000 chars.

    Never stages or reveals secret contents or files claimed by other tasks.
    States plainly which paths commit would refuse.
    Unstages everything before returning.
    """
    candidate_paths = await get_candidate_paths(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
    )

    denylisted = [p for p in candidate_paths if is_secret(p)]
    conflicts: list[str] = []
    if candidate_paths:
        async with engine.connect() as conn:
            stmt = (
                select(file_claims.c.path)
                .join(tasks, tasks.c.id == file_claims.c.task_id)
                .where(
                    tasks.c.status.in_(("active", "in_review")),
                    tasks.c.id != task.id,
                    file_claims.c.path.in_(candidate_paths),
                )
            )
            conflicts = [str(r) for r in (await conn.execute(stmt)).scalars().all()]

    valid_paths = [p for p in candidate_paths if p not in denylisted and p not in conflicts]

    diff = ""
    if valid_paths:
        try:
            await task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["add", "--", *valid_paths],
                extra_env={"GIT_LITERAL_PATHSPECS": "1"},
            )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(select(tasks.c.base_sha).where(tasks.c.id == task.id))
                ).scalar_one_or_none()
            base_sha = row or "HEAD"

            diff_res = await task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["diff", "--cached", base_sha],
            )
            diff = diff_res.stdout
        finally:

            async def _reset() -> None:
                try:
                    await task_git(
                        runner,
                        git_dir=git_dir,
                        worktree=worktree,
                        empty_hooks=empty_hooks,
                        args=["reset"],
                        check=False,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to reset working tree during staged_diff cleanup: %s",
                        exc,
                        exc_info=True,
                    )

            await asyncio.shield(_reset())

    notices: list[str] = []
    if denylisted:
        notices.append(f"Commit will refuse: secret files present ({', '.join(denylisted)}).")
    if conflicts:
        notices.append(
            f"Commit will refuse: files claimed by another task ({', '.join(conflicts)})."
        )

    if notices:
        header = "\n".join(notices) + "\n\n"
        full_diff = header + diff
    else:
        full_diff = diff

    return cap_diff(full_diff)
