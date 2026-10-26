from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.workspace.refs import RepoRef, github_remote

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.models import Project


async def resolve_repo_ref(
    runner: GitRunner, *, project: Project, repo_root: Path, empty_hooks: Path
) -> RepoRef | None:
    """The GitHub repository a project pushes to: its linked repository, else its origin.

    `remote get-url` is the fallback because it applies `url.<base>.insteadOf` rewrites that
    the raw `remote.origin.url` value does not.
    """
    if project.github_full_name:
        return github_remote(project, None)

    for args in (["config", "--get", "remote.origin.url"], ["remote", "get-url", "origin"]):
        res = await run_safe_git(
            runner, args=args, cwd=repo_root, empty_hooks=empty_hooks, check=False
        )
        ref = github_remote(project, res.stdout.strip() if res.code == 0 else None)
        if ref is not None:
            return ref
    return None
