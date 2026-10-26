from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tasks.git import task_git
from mlops_agents.storage.tables import integrations

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.storage.databases import Databases


async def get_github_login(databases: Databases) -> str | None:
    engine = await databases.app()
    async with engine.connect() as conn:
        res = await conn.execute(
            select(integrations.c.login).where(integrations.c.name == "github")
        )
        login = res.scalar_one_or_none()
        if login and login.strip():
            return login.strip()
    return None


async def resolve_commit_author(
    databases: Databases,
    runner: GitRunner,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
) -> tuple[str, str]:
    """Resolves commit author name and email.

    Uses user.name and user.email from git config. If not configured,
    falls back to the authenticated GitHub integration login. Never invents a generic identity
    and refuses with CommitRefused if no identity can be determined.
    """
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

    if not user_name or not user_email:
        login = await get_github_login(databases)
        if not login:
            raise CommitRefused(
                "Cannot determine commit author: git user.name and user.email are not configured, "
                "and no GitHub integration login is available."
            )
        return login, f"{login}@users.noreply.github.com"

    return user_name, user_email
