"""Local merge of a task branch into the default branch (spec §6)."""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mlops_agents.agents.tasks.author import resolve_commit_author
from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tasks.git import run_safe_git

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.storage.databases import Databases


def _make_writable(path: Path) -> None:
    if os.name == "nt":
        with suppress(Exception):
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)  # FILE_ATTRIBUTE_NORMAL
    with suppress(Exception):
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


def _rmtree(path: Path) -> None:
    def on_exc(func: Any, target: str, exc: Any) -> None:
        del exc
        _make_writable(Path(target))
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=on_exc)


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

    temp_worktree = temp_base_dir / f"merge_{task_id}"
    await asyncio.to_thread(temp_base_dir.mkdir, parents=True, exist_ok=True)

    try:
        # Step 1: Create detached temp worktree on default_branch
        old_sha_res = await run_safe_git(
            runner,
            args=["rev-parse", f"refs/heads/{default_branch}"],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        old_sha = old_sha_res.stdout.strip()

        await run_safe_git(
            runner,
            args=["worktree", "add", "--detach", str(temp_worktree), default_branch],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )

        merge_msg = f"Merge task {task_id} into {default_branch}"
        merge_res = await run_safe_git(
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
            diff_res = await run_safe_git(
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

        rev_res = await run_safe_git(
            runner,
            args=["rev-parse", "HEAD"],
            cwd=temp_worktree,
            empty_hooks=empty_hooks,
        )
        merge_sha = rev_res.stdout.strip()

        # Step 2: In user's repository
        wt_list_res = await run_safe_git(
            runner,
            args=["worktree", "list", "--porcelain"],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        checked_out_wt = _find_checked_out_worktree(
            wt_list_res.stdout, default_branch, exclude=temp_worktree
        )

        if checked_out_wt is not None:
            ff_res = await run_safe_git(
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
            ref_res = await run_safe_git(
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
        # Step 3: Clean up temporary worktree
        with suppress(Exception):
            await run_safe_git(
                runner,
                args=["worktree", "remove", "--force", str(temp_worktree)],
                cwd=repo_root,
                empty_hooks=empty_hooks,
                check=False,
            )
        await asyncio.to_thread(_rmtree, temp_worktree)
