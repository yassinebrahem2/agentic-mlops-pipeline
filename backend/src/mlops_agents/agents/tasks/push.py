from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.tasks.git import task_git
from mlops_agents.config import APP_NAME
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import tasks
from mlops_agents.workspace.refs import RepoRef, github_remote, https_url, repo_ref

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.models import Project


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

    # Push without -u to avoid modifying .git/config
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["push", https_url(repo_ref_val), f"{task.branch}:{task.branch}"],
        authenticated=True,
    )

    # Record pushed commit under namespaced ref refs/<app name>/pushed/<branch>
    sha_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-parse", task.branch],
    )
    pushed_ref = f"refs/{APP_NAME}/pushed/{task.branch}"
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["update-ref", pushed_ref, sha_res.stdout.strip()],
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
    if not task.worktree.exists():
        return False

    # Check worktree cleanliness
    status_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["status", "--porcelain"],
    )
    if bool(status_res.stdout.strip()):
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
                check=False,
            )
            return bool(rev_list_res.stdout.strip())

        rev_list_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-list", f"{pushed_ref}..{task.branch}"],
            check=False,
        )
        return bool(rev_list_res.stdout.strip())

    # Project has NO RepoRef
    rev_list_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-list", f"{base_sha}..{task.branch}"],
        check=False,
    )
    if not bool(rev_list_res.stdout.strip()):
        return False

    return not (task_row["status"] == "in_review" and task_row["review_kind"] == "local")
