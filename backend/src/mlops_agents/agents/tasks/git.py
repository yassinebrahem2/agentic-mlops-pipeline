from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitResult, GitRunner


def git_dir_for_task(repo_root: Path, task_id: str) -> Path:
    """Finds the worktree git dir for a task in repo_root.

    Never trusts the worktree's own .git entry. Instead, resolves through the
    repository's own git directory.
    """
    git_entry = repo_root / ".git"
    if git_entry.is_file():
        line = git_entry.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            target = Path(line.removeprefix("gitdir:").strip())
            if not target.is_absolute():
                target = (repo_root / target).resolve()
            common_file = target / "commondir"
            if common_file.exists():
                common = common_file.read_text(encoding="utf-8").strip()
                common_dir = (target / common).resolve()
                return common_dir / "worktrees" / task_id
            return target / "worktrees" / task_id
    return git_entry / "worktrees" / task_id


async def run_safe_git(
    runner: GitRunner,
    *,
    args: Sequence[str],
    cwd: Path,
    empty_hooks: Path,
    git_dir: Path | None = None,
    worktree: Path | None = None,
    check: bool = True,
    timeout: float = 600,  # noqa: ASYNC109 -- passed to subprocess.run in asyncio.to_thread
    authenticated: bool = False,
    extra_env: dict[str, str] | None = None,
) -> GitResult:
    """Runs git safely on a repository or task worktree.

    Safety measures applied on every call:
    - empty hooksPath pointing to data folder empty-hooks;
    - core.fsmonitor=false and core.longpaths=true;
    - GIT_SSH_COMMAND="ssh -o BatchMode=yes";
    - token routed strictly through GIT_ASKPASS;
    - clears inherited GIT_AUTHOR_* and GIT_COMMITTER_* from environment;
    - scrubs output of secrets and tokens.
    """
    merged_env: dict[str, str] = dict(extra_env) if extra_env else {}
    if git_dir is not None:
        merged_env["GIT_DIR"] = str(git_dir)
    if worktree is not None:
        merged_env["GIT_WORK_TREE"] = str(worktree)

    safe_args = [
        "-c",
        f"core.hooksPath={empty_hooks}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.longpaths=true",
        *args,
    ]

    return await runner.run(
        safe_args,
        cwd=cwd,
        check=check,
        timeout=timeout,
        authenticated=authenticated,
        extra_env=merged_env if merged_env else None,
    )


async def task_git(
    runner: GitRunner,
    *,
    git_dir: Path,
    worktree: Path,
    empty_hooks: Path,
    args: Sequence[str],
    check: bool = True,
    timeout: float = 600,  # noqa: ASYNC109 -- passed to subprocess.run in asyncio.to_thread
    authenticated: bool = False,
    extra_env: dict[str, str] | None = None,
) -> GitResult:
    """The only code that runs git on a task worktree."""
    worktree_exists = await asyncio.to_thread(worktree.exists)
    return await run_safe_git(
        runner,
        args=args,
        cwd=worktree if worktree_exists else empty_hooks.parent,
        empty_hooks=empty_hooks,
        git_dir=git_dir,
        worktree=worktree if worktree_exists else None,
        check=check,
        timeout=timeout,
        authenticated=authenticated,
        extra_env=extra_env,
    )
