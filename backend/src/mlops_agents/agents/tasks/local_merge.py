"""Local merge of a task branch into the default branch (spec §6)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.tasks.author import resolve_commit_author
from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.integrations.github.git import GitError
from mlops_agents.workspace.fs import rmtree

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mlops_agents.integrations.github.git import GitResult, GitRunner
    from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


async def _git_to_end(
    runner: GitRunner,
    *,
    args: Sequence[str],
    cwd: Path,
    empty_hooks: Path,
    check: bool = True,
) -> GitResult:
    """Runs git and, when cancelled, waits for that git process before cancelling.

    git runs in a worker thread that a cancel cannot stop, so without the wait the cleanup
    would remove the temporary worktree under a git process still writing to it.
    """
    call = asyncio.ensure_future(
        run_safe_git(runner, args=args, cwd=cwd, empty_hooks=empty_hooks, check=check)
    )
    try:
        return await asyncio.shield(call)
    except asyncio.CancelledError:
        await asyncio.wait({call})
        if not call.cancelled() and (exc := call.exception()) is not None:
            logger.warning("git %s failed after the merge was cancelled", args[0], exc_info=exc)
        raise


def _find_checked_out_worktree(
    porcelain: str,
    default_branch: str,
    exclude: Path | None = None,
) -> Path | None:
    target_ref = f"refs/heads/{default_branch}"
    current_path: Path | None = None
    exclude_resolved = exclude.resolve() if exclude is not None else None

    for line in porcelain.splitlines():
        line = line.strip()
        if not line:
            current_path = None
            continue
        if line.startswith("worktree "):
            raw_path = line.removeprefix("worktree ").strip()
            current_path = Path(raw_path)
        elif line.startswith("branch "):
            ref = line.removeprefix("branch ").strip()
            if (
                ref == target_ref
                and current_path is not None
                and (exclude_resolved is None or current_path.resolve() != exclude_resolved)
            ):
                return current_path
    return None


async def local_merge_preview(
    runner: GitRunner,
    repo_root: Path,
    empty_hooks: Path,
    default_branch: str,
    task_branch: str,
) -> list[str]:
    """Runs git diff --name-only <default_branch>...<task_branch> in repo_root with empty_hooks.

    Returns the list of relative file paths the merge would change.
    """
    res = await run_safe_git(
        runner,
        args=["diff", "--name-only", f"{default_branch}...{task_branch}"],
        cwd=repo_root,
        empty_hooks=empty_hooks,
    )
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


async def local_merge(
    *,
    databases: Databases,
    runner: GitRunner,
    repo_root: Path,
    empty_hooks: Path,
    temp_base_dir: Path,
    default_branch: str,
    task_branch: str,
    task_id: str,
) -> None:
    """Merges task_branch into default_branch following spec section 6:

    1. Resolves commit author identity (human from git config or GitHub login fallback).
    2. Builds merge commit in a temporary worktree in the data folder.
       A conflict raises CommitRefused naming the conflicting paths, leaving user repo untouched.
    3. In the user repository:
       - If default_branch is checked out in any worktree W, run git merge --ff-only <commit> in W.
         If git refuses (e.g. uncommitted changes in the way), raises CommitRefused.
       - If checked out in none of the repository's worktrees, updates branch ref with git update-ref.
    4. Removes the temporary worktree in finally.
    """
    git_dir = repo_root / ".git"
    if git_dir.is_file():
        line = git_dir.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            target = Path(line.removeprefix("gitdir:").strip())
            git_dir = target if target.is_absolute() else (repo_root / target).resolve()

    author_name, author_email = await resolve_commit_author(
        databases=databases,
        runner=runner,
        git_dir=git_dir,
        worktree=repo_root,
        empty_hooks=empty_hooks,
    )

    await asyncio.to_thread(temp_base_dir.mkdir, parents=True, exist_ok=True)
    # A fresh folder per merge: a retry never meets what an earlier merge left behind.
    temp_worktree = Path(
        await asyncio.to_thread(tempfile.mkdtemp, prefix=f"merge_{task_id}_", dir=temp_base_dir)
    )
    added = False

    try:
        # Step 1: Create detached temp worktree on default_branch
        old_sha_res = await _git_to_end(
            runner,
            args=["rev-parse", f"refs/heads/{default_branch}"],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        old_sha = old_sha_res.stdout.strip()

        await _git_to_end(
            runner,
            args=["worktree", "add", "--detach", str(temp_worktree), default_branch],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        added = True

        merge_msg = f"Merge task {task_id} into {default_branch}"
        merge_res = await _git_to_end(
            runner,
            args=[
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "merge",
                "--no-ff",
                "-m",
                merge_msg,
                task_branch,
            ],
            cwd=temp_worktree,
            empty_hooks=empty_hooks,
            check=False,
        )

        if merge_res.code != 0:
            diff_res = await _git_to_end(
                runner,
                args=["diff", "--name-only", "--diff-filter=U"],
                cwd=temp_worktree,
                empty_hooks=empty_hooks,
                check=False,
            )
            conflicts = [p.strip() for p in diff_res.stdout.splitlines() if p.strip()]
            if conflicts:
                raise CommitRefused(f"Merge conflict in: {', '.join(conflicts)}")
            err_msg = merge_res.stderr.strip() or merge_res.stdout.strip() or "Git merge failed."
            raise CommitRefused(err_msg)

        rev_res = await _git_to_end(
            runner,
            args=["rev-parse", "HEAD"],
            cwd=temp_worktree,
            empty_hooks=empty_hooks,
        )
        merge_sha = rev_res.stdout.strip()

        # Step 2: In user's repository
        wt_list_res = await _git_to_end(
            runner,
            args=["worktree", "list", "--porcelain"],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        checked_out_wt = _find_checked_out_worktree(
            wt_list_res.stdout, default_branch, exclude=temp_worktree
        )

        if checked_out_wt is not None:
            ff_res = await _git_to_end(
                runner,
                args=["merge", "--ff-only", merge_sha],
                cwd=checked_out_wt,
                empty_hooks=empty_hooks,
                check=False,
            )
            if ff_res.code != 0:
                err_msg = ff_res.stderr.strip() or ff_res.stdout.strip() or "Fast-forward refused."
                raise CommitRefused(err_msg)
        else:
            ref_res = await _git_to_end(
                runner,
                args=["update-ref", f"refs/heads/{default_branch}", merge_sha, old_sha],
                cwd=repo_root,
                empty_hooks=empty_hooks,
                check=False,
            )
            if ref_res.code != 0:
                raise CommitRefused(
                    f"Fast-forward refused: default branch {default_branch} changed concurrently."
                )

    finally:
        await _remove_temp_worktree(runner, repo_root, empty_hooks, temp_worktree, added=added)


async def _remove_temp_worktree(
    runner: GitRunner, repo_root: Path, empty_hooks: Path, temp_worktree: Path, *, added: bool
) -> None:
    """Removes the merge's temporary worktree; a failure is logged, never raised, so it cannot
    replace the merge's own outcome."""
    if added:
        try:
            res = await _git_to_end(
                runner,
                args=["worktree", "remove", "--force", str(temp_worktree)],
                cwd=repo_root,
                empty_hooks=empty_hooks,
                check=False,
            )
        except GitError:
            logger.exception("Could not remove the temporary merge worktree %s", temp_worktree)
        else:
            if res.code != 0:
                logger.warning(
                    "Could not remove the temporary merge worktree %s: %s",
                    temp_worktree,
                    res.stderr.strip() or res.stdout.strip(),
                )
    try:
        await asyncio.to_thread(rmtree, temp_worktree)
    except OSError:
        logger.exception("Could not delete the temporary merge folder %s", temp_worktree)
