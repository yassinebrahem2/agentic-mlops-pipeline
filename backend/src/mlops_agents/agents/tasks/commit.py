from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.tasks.claims import claim_paths
from mlops_agents.agents.tasks.contract import CommitRefused, CommitResult
from mlops_agents.agents.tasks.git import task_git
from mlops_agents.agents.tools.files import is_secret
from mlops_agents.errors import DomainError
from mlops_agents.storage.tables import file_claims, integrations, tasks
from mlops_agents.workspace.refs import RepoRef, github_remote, https_url, repo_ref

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.storage.databases import Databases
    from mlops_agents.workspace.models import Project

_AI_KEYWORDS = (
    "claude",
    "anthropic",
    "copilot",
    "chatgpt",
    "openai",
    "gemini",
    "assistant",
    "bot",
    "github-actions",
)


def _is_ai_identity(name: str, email: str) -> bool:
    combined = f"{name} {email}".lower()
    return any(keyword in combined for keyword in _AI_KEYWORDS)


async def _get_github_login(databases: Databases) -> str | None:
    engine = await databases.app()
    async with engine.connect() as conn:
        res = await conn.execute(
            select(integrations.c.login).where(integrations.c.name == "github")
        )
        login = res.scalar_one_or_none()
        if login and login.strip():
            return login.strip()
    return None


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
    # 1. Stage everything
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["add", "-A"],
    )

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
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
        )
        raise CommitRefused(
            f"Cannot commit secret files: {', '.join(secrets)}. Delete them from the worktree first."
        )

    # 4. Claim conflict check
    async with engine.connect() as conn:
        stmt = (
            select(file_claims.c.path, tasks.c.id, tasks.c.agent)
            .join(tasks, tasks.c.id == file_claims.c.task_id)
            .where(
                tasks.c.status.in_(("active", "in_review")),
                tasks.c.id != task.id,
                file_claims.c.path.in_(staged_paths),
            )
        )
        conflicts = (await conn.execute(stmt)).mappings().all()

    if conflicts:
        conflict = conflicts[0]
        conflicting_path = str(conflict["path"])
        other_task_id = str(conflict["id"])
        other_agent = str(conflict["agent"])
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
        )
        raise CommitRefused(
            f"Cannot commit {conflicting_path}: claimed by agent {other_agent!r} in task {other_task_id}."
        )

    # 5. Claim every staged path
    await claim_paths(engine, task, staged_paths)

    # 6. Determine user identity
    name_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["config", "user.name"],
        check=False,
    )
    user_name = name_res.stdout.strip() if name_res.code == 0 else ""

    email_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["config", "user.email"],
        check=False,
    )
    user_email = email_res.stdout.strip() if email_res.code == 0 else ""

    if not user_name or not user_email or _is_ai_identity(user_name, user_email):
        login = await _get_github_login(databases) or "user"
        author_name = login
        author_email = f"{login}@users.noreply.github.com"
    else:
        author_name = user_name
        author_email = user_email

    # 7. Commit
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

    # 8. Read SHA
    sha_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-parse", "HEAD"],
    )
    sha = sha_res.stdout.strip()

    # 9. Return CommitResult
    return CommitResult(sha=sha, changed=tuple(staged_paths))


async def staged_diff_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    engine: AsyncEngine,
    task: TaskRef,
) -> str:
    # 1. Stage everything
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["add", "-A"],
    )

    # 2. Read base_sha
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(tasks.c.base_sha).where(tasks.c.id == task.id))
        ).scalar_one_or_none()
    base_sha = row or "HEAD"

    try:
        # 3. Diff against base
        diff_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["diff", "--cached", base_sha],
        )
        diff = diff_res.stdout
    finally:
        # 4. Unstage
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["reset"],
        )

    # 5. Cap output at 60,000 characters
    if len(diff) > 60000:
        diff = diff[:60000] + "\n[diff cut]"
    return diff


async def push_task(
    *,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    project: Project,
    task: TaskRef,
) -> None:
    repo_ref_val = await project_repo_ref(
        runner=runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        project=project,
    )
    if repo_ref_val is None:
        raise DomainError("no_remote", "Project has no GitHub remote to push to.", status=409)

    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["push", "-u", https_url(repo_ref_val), f"{task.branch}:{task.branch}"],
        authenticated=True,
    )

    sha_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["rev-parse", task.branch],
    )
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree,
        empty_hooks=empty_hooks,
        args=["update-ref", f"refs/remotes/origin/{task.branch}", sha_res.stdout.strip()],
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
        verify_res = await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree,
            empty_hooks=empty_hooks,
            args=["rev-parse", "--verify", f"refs/remotes/origin/{task.branch}"],
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
            args=["rev-list", f"refs/remotes/origin/{task.branch}..{task.branch}"],
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
