from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.tasks.author import resolve_commit_author
from mlops_agents.agents.tasks.claims import claim_paths, release_claims
from mlops_agents.agents.tasks.contract import ClaimConflict, CommitRefused, CommitResult
from mlops_agents.agents.tasks.diff import get_candidate_paths, staged_diff_task
from mlops_agents.agents.tasks.git import task_git
from mlops_agents.agents.tasks.push import has_unpublished_changes_task, push_task
from mlops_agents.agents.tools.files import is_secret
from mlops_agents.storage.tables import file_claims

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

logger = logging.getLogger(__name__)


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
    committed = False
    head_before: str | None = None
    newly_claimed_paths: list[str] = []
    try:
        # Check existing claims by this task to compute newly_claimed_paths
        async with engine.connect() as conn:
            stmt = select(file_claims.c.path).where(
                file_claims.c.task_id == task.id,
                file_claims.c.path.in_(candidate_paths),
            )
            existing_claims = set((await conn.execute(stmt)).scalars().all())
        newly_claimed_paths = [p for p in candidate_paths if p not in existing_claims]

        try:
            await claim_paths(engine, task, candidate_paths)
        except ClaimConflict as exc:
            raise CommitRefused(f"Cannot commit: {exc}") from None

        # 5. Stage, commit, and inspect commit inside guarded block
        add_fut = asyncio.create_task(
            task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["add", "--", *candidate_paths],
                extra_env={"GIT_LITERAL_PATHSPECS": "1"},
            )
        )
        try:
            await asyncio.shield(add_fut)
        except asyncio.CancelledError:
            with suppress(Exception):
                await add_fut
            raise

        head_before_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "HEAD"],
            check=False,
        )
        head_before = head_before_res.stdout.strip() if head_before_res.code == 0 else ""

        commit_task_fut = asyncio.create_task(
            task_git(
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
                check=False,
            )
        )
        try:
            commit_res = await asyncio.shield(commit_task_fut)
        except asyncio.CancelledError:
            with suppress(Exception):
                await commit_task_fut
            raise

        if commit_res.code != 0:
            raise CommitRefused(f"Git commit failed: {commit_res.stderr or commit_res.stdout}")
        committed = True

        sha_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "HEAD"],
        )
        sha = sha_res.stdout.strip()
        return CommitResult(sha=sha, changed=tuple(candidate_paths))

    finally:
        if not committed and head_before is not None:
            head_now_res = await task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["rev-parse", "HEAD"],
                check=False,
            )
            if head_now_res.code == 0 and head_now_res.stdout.strip() != head_before:
                committed = True

        if not committed:

            async def _cleanup() -> None:
                try:
                    reset_res = await task_git(
                        runner,
                        git_dir=git_dir,
                        worktree=worktree,
                        empty_hooks=empty_hooks,
                        args=["reset"],
                        check=False,
                    )
                    if reset_res.code != 0:
                        logger.warning(
                            "Failed to reset working tree during commit cleanup (exit code %d): %s",
                            reset_res.code,
                            reset_res.stderr.strip() or reset_res.stdout.strip(),
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to reset working tree during commit cleanup: %s",
                        exc,
                        exc_info=True,
                    )

                if newly_claimed_paths:
                    try:
                        await release_claims(engine, task.id, newly_claimed_paths)
                    except Exception as exc:
                        logger.warning(
                            "Failed to release claims during commit cleanup: %s",
                            exc,
                            exc_info=True,
                        )

            await asyncio.shield(_cleanup())
