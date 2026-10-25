from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.tasks.author import resolve_commit_author
from mlops_agents.agents.tasks.claims import claim_paths
from mlops_agents.agents.tasks.contract import ClaimConflict, CommitRefused, CommitResult
from mlops_agents.agents.tasks.diff import staged_diff_task
from mlops_agents.agents.tasks.git import task_git
from mlops_agents.agents.tasks.push import has_unpublished_changes_task, push_task
from mlops_agents.agents.tools.files import is_secret

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.storage.databases import Databases

__all__ = [
    "CommitRefused",
    "CommitResult",
    "commit_task",
    "has_unpublished_changes_task",
    "push_task",
    "staged_diff_task",
]


async def commit_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    databases: Databases,
    engine: AsyncEngine,
    task: TaskRef,
    message: str,
) -> CommitResult:
    """Commits changes staged in the task worktree.

    - Stages all changes with git add -A.
    - Validates no staged files are denylisted (is_secret).
    - Checks and records claims atomically (rolling back and raising CommitRefused on conflict).
    - Commits using resolved author identity (never an AI identity).
    - Unstages all changes on any error so a failed commit leaves nothing staged and no claims.
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
        diff_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["diff", "--cached", "--name-only", "-z"],
        )
        staged_paths = [p for p in diff_res.stdout.split("\0") if p]
        if not staged_paths:
            raise CommitRefused("Nothing to commit: working tree clean.")

        # 3. Denylist check
        secrets = [p for p in staged_paths if is_secret(p)]
        if secrets:
            raise CommitRefused(
                f"Cannot commit secret files: {', '.join(secrets)}. Delete them from the worktree first."
            )

        # 4. Check & claim paths atomically
        try:
            await claim_paths(engine, task, staged_paths)
        except ClaimConflict as exc:
            raise CommitRefused(f"Cannot commit: {exc}") from None

        # 5. Determine author identity
        author_name, author_email = await resolve_commit_author(
            databases, runner, git_dir, worktree, empty_hooks
        )

        # 6. Commit
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=[
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "commit",
                f"--author={author_name} <{author_email}>",
                "-m",
                message,
            ],
        )

        # 7. Read SHA
        sha_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "HEAD"],
        )
        sha = sha_res.stdout.strip()
        return CommitResult(sha=sha, changed=tuple(staged_paths))

    except Exception:
        # Always unstage on any failure path
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
            check=False,
        )
        raise
