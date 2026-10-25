from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.tasks.author import resolve_commit_author
from mlops_agents.agents.tasks.claims import claim_paths, release_claims
from mlops_agents.agents.tasks.contract import ClaimConflict, CommitRefused, CommitResult
from mlops_agents.agents.tasks.diff import get_candidate_paths, staged_diff_task
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
    """Commits changes in the task worktree.

    - Resolves commit author identity first (never an AI identity).
    - Discovers candidate paths without staging, preventing secret blobs from entering the object store.
    - Validates no candidate files are denylisted (is_secret).
    - Checks and records claims atomically (raising CommitRefused on conflict).
    - Stages candidate files and commits inside a guarded block.
    - Releases claims and unstages all changes on any error so a failed commit leaves
      no claims and nothing staged.
    """
    # 1. Determine author identity first
    author_name, author_email = await resolve_commit_author(
        databases, runner, git_dir, worktree, empty_hooks
    )

    # 2. Build candidate paths list without staging
    candidate_paths = await get_candidate_paths(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
    )
    if not candidate_paths:
        raise CommitRefused("Nothing to commit: working tree clean.")

    # 3. Denylist check before staging (never enters git object store)
    secrets = [p for p in candidate_paths if is_secret(p)]
    if secrets:
        raise CommitRefused(
            f"Cannot commit secret files: {', '.join(secrets)}. Delete them from the worktree first."
        )

    # 4. Check & claim paths atomically
    try:
        await claim_paths(engine, task, candidate_paths)
    except ClaimConflict as exc:
        raise CommitRefused(f"Cannot commit: {exc}") from None

    # 5. Stage, commit, and inspect commit inside guarded block
    try:
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["add", "--", *candidate_paths],
        )

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

        sha_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "HEAD"],
        )
        sha = sha_res.stdout.strip()
        return CommitResult(sha=sha, changed=tuple(candidate_paths))

    except Exception:
        # Release claims and unstage on any failure path
        await release_claims(engine, task.id, candidate_paths)
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
            check=False,
        )
        raise
