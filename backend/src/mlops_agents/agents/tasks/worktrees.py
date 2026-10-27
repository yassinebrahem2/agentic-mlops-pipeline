from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import stat
from pathlib import Path

from mlops_agents.agents.tasks.git import git_dir_for_task, run_safe_git, task_git
from mlops_agents.agents.tasks.remote import resolve_repo_ref
from mlops_agents.agents.temp_files import is_generated_temp_file
from mlops_agents.agents.tools.base import FileAccess, ToolFailure
from mlops_agents.agents.tools.files import is_secret, norm_path
from mlops_agents.config import APP_NAME
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.refs import https_url

logger = logging.getLogger(__name__)

_NOT_REGULAR = frozenset({"120000", "160000"})


def title_slug(title: str) -> str:
    """Slug from title: lowercase ASCII, hyphens, at most 40 characters."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-") or "task"


def task_branch_name(agent: str, task_id: str, title: str) -> str:
    """Branch name follows agent/<agent>/<slug>-<6 chars of task id>."""
    slug = title_slug(title)
    short_id = task_id.removeprefix("tsk_")[:6]
    return f"agent/{agent}/{slug}-{short_id}"


def _make_writable(path: Path) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)  # FILE_ATTRIBUTE_NORMAL
    path.chmod(stat.S_IWRITE | stat.S_IREAD)


async def create_worktree(
    *,
    runner: GitRunner,
    project: Project,
    repo_root: Path,
    worktree_path: Path,
    empty_hooks: Path,
    task_id: str,
    agent: str,
    title: str,
) -> tuple[str, str, str]:
    """Creates a worktree for a task, returning (branch, base_ref, base_sha).

    1. Determines remote link (RepoRef):
       - For GitHub projects: github_full_name.
       - For local projects: reads origin URL from repository.
    2. If RepoRef exists:
       - Fetches remote default branch into refs/<APP_NAME>/base/<default_branch>.
       - Leaves user's origin/* refs untouched.
       - base_ref is refs/<APP_NAME>/base/<default_branch>.
       Else:
       - base_ref is local default branch.
    3. Resolves base_sha from base_ref.
    4. Runs `git worktree add -b <branch> <worktree_path> <base_ref>`.
    5. Strips secret files: runs `git update-index --skip-worktree` and removes them.
    """
    remote_ref = await resolve_repo_ref(
        runner, project=project, repo_root=repo_root, empty_hooks=empty_hooks
    )

    default_branch = project.default_branch or "main"
    if remote_ref is not None:
        namespaced_ref = f"refs/{APP_NAME}/base/{default_branch}"
        fetch_refspec = f"+refs/heads/{default_branch}:{namespaced_ref}"
        await run_safe_git(
            runner,
            args=["fetch", https_url(remote_ref), fetch_refspec],
            cwd=repo_root,
            empty_hooks=empty_hooks,
            authenticated=True,
        )
        base_ref = namespaced_ref
    else:
        base_ref = default_branch

    sha_res = await run_safe_git(
        runner,
        args=["rev-parse", base_ref],
        cwd=repo_root,
        empty_hooks=empty_hooks,
    )
    base_sha = sha_res.stdout.strip()

    branch = task_branch_name(agent, task_id, title)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    await run_safe_git(
        runner,
        args=["worktree", "add", "-b", branch, str(worktree_path), base_ref],
        cwd=repo_root,
        empty_hooks=empty_hooks,
    )

    await strip_secret_files(
        runner=runner,
        repo_root=repo_root,
        task_id=task_id,
        worktree_path=worktree_path,
        empty_hooks=empty_hooks,
    )

    return branch, base_ref, base_sha


async def strip_secret_files(
    *,
    runner: GitRunner,
    repo_root: Path,
    task_id: str,
    worktree_path: Path,
    empty_hooks: Path,
) -> None:
    git_dir = git_dir_for_task(repo_root, task_id)
    ls_res = await task_git(
        runner,
        git_dir=git_dir,
        worktree=worktree_path,
        empty_hooks=empty_hooks,
        args=["ls-files", "-z"],
    )
    tracked_files = [p for p in ls_res.stdout.split("\0") if p]
    secret_files = [p for p in tracked_files if is_secret(p)]

    if secret_files:
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=worktree_path,
            empty_hooks=empty_hooks,
            args=["update-index", "--skip-worktree", "--", *secret_files],
        )
        for rel in secret_files:
            file_on_disk = worktree_path / rel
            if file_on_disk.exists() or file_on_disk.is_symlink():
                _make_writable(file_on_disk)
                file_on_disk.unlink()


async def recreate_worktree(
    *,
    runner: GitRunner,
    repo_root: Path,
    worktree_path: Path,
    empty_hooks: Path,
    task_id: str,
    branch: str,
) -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await run_safe_git(
        runner,
        args=["worktree", "prune"],
        cwd=repo_root,
        empty_hooks=empty_hooks,
        check=False,
    )
    await run_safe_git(
        runner,
        args=["worktree", "add", "--force", str(worktree_path), branch],
        cwd=repo_root,
        empty_hooks=empty_hooks,
    )
    await strip_secret_files(
        runner=runner,
        repo_root=repo_root,
        task_id=task_id,
        worktree_path=worktree_path,
        empty_hooks=empty_hooks,
    )


class TaskFiles(FileAccess):
    """File access in a task's worktree: regular files tracked by git plus untracked files,
    minus secrets and ignored files, never outside the worktree root.
    """

    def __init__(
        self,
        root: Path,
        git_dir: Path,
        empty_hooks: Path,
        runner: GitRunner,
    ) -> None:
        self._root = root
        self._resolved_root: Path | None = None
        self._git_dir = git_dir
        self._empty_hooks = empty_hooks
        self._runner = runner
        self._tracked: list[str] | None = None
        self._lookup: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    async def tracked(self) -> list[str]:
        if self._tracked is None:
            result = await task_git(
                self._runner,
                git_dir=self._git_dir,
                worktree=self._root,
                empty_hooks=self._empty_hooks,
                args=["ls-files", "-s", "-z", "--cached", "--others", "--exclude-standard"],
            )
            paths: list[str] = []
            for entry in result.stdout.split("\0"):
                if not entry:
                    continue
                meta, sep, path = entry.partition("\t")
                if sep:
                    mode = meta.split(" ", 1)[0]
                    if mode not in _NOT_REGULAR:
                        paths.append(path)
                else:
                    paths.append(entry)
            self._tracked = sorted({p for p in paths if not is_secret(p)})
            self._lookup = {norm_path(p): p for p in self._tracked}
        return self._tracked

    def invalidate(self) -> None:
        self._tracked = None
        self._lookup = {}

    async def under(self, prefix: str) -> list[str]:
        files = await self.tracked()
        if not prefix:
            return files
        wanted = norm_path(prefix)
        return [f for f in files if norm_path(f).startswith(wanted)]

    def _relative(self, user_path: str) -> str:
        if self._resolved_root is None:
            self._resolved_root = self._root.resolve()
        candidate = Path(user_path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._resolved_root):
            raise ToolFailure(
                "That path is outside the repository. Only files inside the project can be read."
            )
        return resolved.relative_to(self._resolved_root).as_posix()

    async def resolve_file(self, user_path: str) -> str:
        rel = await asyncio.to_thread(self._relative, user_path)
        if is_secret(rel):
            raise ToolFailure(f"{rel} looks like a secret file, so it cannot be read.")
        await self.tracked()
        actual = self._lookup.get(norm_path(rel))
        if actual is None:
            raise ToolFailure(
                f"{rel} is not a file in this worktree. Only tracked or new files can be "
                "read; use Glob to find files."
            )
        return actual

    async def resolve_dir(self, user_path: str | None) -> str:
        if not user_path or user_path.strip() in (".", "./"):
            return ""
        rel = await asyncio.to_thread(self._relative, user_path)
        return "" if rel == "." else rel.rstrip("/") + "/"


def clean_leftover_temp_files(worktree_path: Path) -> None:
    """Removes leftover atomic write temp files matching .tmp_<name>_<32 hex>."""
    if not worktree_path.is_dir():
        return
    for item in worktree_path.rglob(".tmp_*"):
        if ".git" in item.parts or not item.is_file():
            continue
        if is_generated_temp_file(item):
            try:
                item.unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to unlink leftover temp file %s: %s",
                    item,
                    exc,
                    exc_info=True,
                )
