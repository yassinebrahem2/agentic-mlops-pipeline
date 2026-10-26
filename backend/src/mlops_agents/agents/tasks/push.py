from __future__ import annotations

import asyncio
import logging
import shutil
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from mlops_agents.agents.tasks.git import run_safe_git, task_git
from mlops_agents.agents.temp_files import is_generated_temp_file
from mlops_agents.config import APP_NAME
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import tasks
from mlops_agents.workspace.refs import RepoRef, github_remote, https_url, repo_ref

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.models import Project

logger = logging.getLogger(__name__)


def _rmtree(path: Path) -> None:
    def on_exc(func: Any, target: str, exc: Any) -> None:
        try:
            Path(target).chmod(stat.S_IWRITE | stat.S_IREAD)
        except Exception as chmod_exc:
            logger.warning("Failed to reset permissions on %s: %s", target, chmod_exc)
        try:
            func(target)
        except Exception as retry_exc:
            logger.warning("Failed to remove %s: %s", target, retry_exc)

    if path.exists():
        try:
            shutil.rmtree(path, onexc=on_exc)
        except Exception as exc:
            logger.warning("Failed to remove directory %s: %s", path, exc)


async def project_repo_ref(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    project: Project,
) -> RepoRef | None:
    if project.github_full_name:
        return repo_ref(project.github_full_name)

    origin_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["config", "--get", "remote.origin.url"],
        check=False,
    )
    origin_url = origin_res.stdout.strip() if origin_res.code == 0 else None
    ref = github_remote(project, origin_url)
    if ref is None:
        origin_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["remote", "get-url", "origin"],
            check=False,
        )
        origin_url = origin_res.stdout.strip() if origin_res.code == 0 else None
        ref = github_remote(project, origin_url)
    return ref


async def push_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    project: Project,
    task: TaskRef,
) -> None:
    """Pushes the task branch to remote without modifying user git config or origin/* refs."""
    repo_ref_val = await project_repo_ref(
        runner=runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        project=project,
    )
    if repo_ref_val is None:
        raise DomainError("no_remote", "Project has no GitHub remote to push to.", status=409)

    sha_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-parse", f"refs/heads/{task.branch}"],
    )
    commit_sha = sha_res.stdout.strip()

    # Discover the repository object directory so the push can read objects
    # from an isolated bare repository without loading user remotes from .git/config.
    objects_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-parse", "--git-path", "objects"],
    )
    objects_path = Path(objects_res.stdout.strip())
    if not objects_path.is_absolute():
        objects_path = (worktree / objects_path).resolve()

    unique_suffix = uuid.uuid4().hex[:8]
    temp_push = empty_hooks.parent / f".tmp_push_{task.id}_{commit_sha[:8]}_{unique_suffix}"
    push_fut: asyncio.Task[Any] | None = None

    try:
        init_fut = asyncio.create_task(
            run_safe_git(
                runner,
                args=["init", "--bare", str(temp_push)],
                cwd=empty_hooks.parent,
                empty_hooks=empty_hooks,
            )
        )
        try:
            await asyncio.shield(init_fut)
        except asyncio.CancelledError:
            try:
                await init_fut
            except Exception as exc:
                logger.warning("Bare repo init failed during cancellation: %s", exc, exc_info=True)
            raise
        args = ["push", https_url(repo_ref_val), f"{commit_sha}:refs/heads/{task.branch}"]
        push_fut = asyncio.create_task(
            run_safe_git(
                runner,
                args=args,
                cwd=temp_push,
                git_dir=temp_push,
                empty_hooks=empty_hooks,
                authenticated=True,
                extra_env={
                    "GIT_OBJECT_DIRECTORY": str(objects_path),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects_path),
                },
            )
        )
        await asyncio.shield(push_fut)
    except asyncio.CancelledError:
        if push_fut is not None:
            try:
                await asyncio.shield(push_fut)
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    logger.warning("Push failed during cancellation: %s", exc)
        raise
    finally:

        async def _cleanup() -> None:
            try:
                await asyncio.to_thread(_rmtree, temp_push)
            except Exception as exc:
                logger.warning("Failed to clean up temp bare repo %s: %s", temp_push, exc)

        await asyncio.shield(_cleanup())

    pushed_ref = f"refs/{APP_NAME}/pushed/{task.branch}"
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["update-ref", pushed_ref, commit_sha],
    )


async def has_unpublished_changes_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    engine: AsyncEngine,
    project: Project,
    task: TaskRef,
) -> bool:
    if await asyncio.to_thread(worktree.exists):
        # Check worktree cleanliness (ignoring any leftover generated atomic write temp files)
        status_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["status", "--porcelain=v1", "-uall", "-z", "--no-renames"],
        )
        for token in status_res.stdout.split("\0"):
            if len(token) > 3:
                path = token[3:]
                if not is_generated_temp_file(path):
                    return True

    # Read task record from DB
    async with engine.connect() as conn:
        task_row = (
            (
                await conn.execute(
                    select(tasks.c.base_sha, tasks.c.status, tasks.c.review_kind).where(
                        tasks.c.id == task.id
                    )
                )
            )
            .mappings()
            .first()
        )

    if task_row is None:
        return False

    base_sha = task_row["base_sha"] or "HEAD"

    # Determine project RepoRef
    repo_ref_val = await project_repo_ref(
        runner=runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        project=project,
    )

    if repo_ref_val is not None:
        pushed_ref = f"refs/{APP_NAME}/pushed/{task.branch}"
        verify_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "--verify", pushed_ref],
            check=False,
        )
        if verify_res.code != 0:
            rev_list_res = await task_git(
                runner,
                git_dir=git_dir,
                worktree=worktree,
                empty_hooks=empty_hooks,
                args=["rev-list", f"{base_sha}..{task.branch}"],
            )
            return bool(rev_list_res.stdout.strip())

        rev_list_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-list", f"{pushed_ref}..{task.branch}"],
        )
        return bool(rev_list_res.stdout.strip())

    # Project has NO RepoRef
    rev_list_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-list", f"{base_sha}..{task.branch}"],
    )
    if not bool(rev_list_res.stdout.strip()):
        return False

    return not (task_row["status"] == "in_review" and task_row["review_kind"] == "local")
