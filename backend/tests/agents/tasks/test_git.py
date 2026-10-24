from __future__ import annotations

import ctypes
import os
import stat
import subprocess
from pathlib import Path

import pytest

from mlops_agents.agents.tasks.git import git_dir_for_task, task_git
from mlops_agents.integrations.github.git import GitError, GitRunner
from mlops_agents.paths import DataPaths
from tests.conftest import make_repo


def _make_writable(path: Path) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)  # FILE_ATTRIBUTE_NORMAL
    path.chmod(stat.S_IWRITE | stat.S_IREAD)


def _cmd(
    args: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


async def test_task_git_runs_cleanly_in_worktree(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    repo = make_repo(tmp_path / "repo", {"main.py": "print('hello')\n"}, git_env)
    wt = tmp_path / "wt"
    _cmd(["git", "worktree", "add", "-b", "agent/test/task-123456", str(wt)], cwd=repo)
    git_dir = git_dir_for_task(repo, "wt")
    runner = GitRunner(paths.bin)

    result = await task_git(
        runner,
        git_dir=git_dir,
        worktree=wt,
        empty_hooks=paths.empty_hooks,
        args=["status", "--porcelain"],
    )
    assert result.code == 0
    assert result.stdout.strip() == ""


async def test_task_git_ignores_planted_fsmonitor_in_worktree(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    repo = make_repo(tmp_path / "repo", {"main.py": "print('hello')\n"}, git_env)
    wt = tmp_path / "wt"
    _cmd(["git", "worktree", "add", "-b", "agent/test/task-123456", str(wt)], cwd=repo)
    git_dir = git_dir_for_task(repo, "wt")

    # Create a planted malicious repo whose config sets core.fsmonitor to an evil script
    planted = tmp_path / "planted"
    planted.mkdir()
    _cmd(["git", "init"], cwd=planted)
    pwned_file = tmp_path / "pwned.txt"
    if os.name == "nt":
        evil_script = tmp_path / "evil.cmd"
        evil_script.write_text(f'@echo EVIL > "{pwned_file}"\r\n', encoding="utf-8")
    else:
        evil_script = tmp_path / "evil.sh"
        evil_script.write_text(f'#!/bin/sh\necho EVIL > "{pwned_file}"\n', encoding="utf-8")
        evil_script.chmod(0o755)

    _cmd(["git", "config", "core.fsmonitor", str(evil_script)], cwd=planted)

    # Rewrite wt/.git to point to planted malicious repo
    wt_git_file = wt / ".git"
    _make_writable(wt_git_file)
    wt_git_file.write_text(f"gitdir: {planted / '.git'}\n", encoding="utf-8")

    runner = GitRunner(paths.bin)
    result = await task_git(
        runner,
        git_dir=git_dir,
        worktree=wt,
        empty_hooks=paths.empty_hooks,
        args=["status", "--porcelain"],
    )
    assert result.code == 0
    assert not pwned_file.exists()


async def test_task_git_disables_hooks(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n"}, git_env)
    wt = tmp_path / "wt"
    _cmd(["git", "worktree", "add", "-b", "agent/test/task-hooks", str(wt)], cwd=repo)
    git_dir = git_dir_for_task(repo, "wt")

    # Install a pre-commit hook in repo that touches hook_ran.txt if run
    hook_ran = tmp_path / "hook_ran.txt"
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        hook = hooks_dir / "pre-commit.cmd"
        hook.write_text(f'@echo HOOK > "{hook_ran}"\r\n', encoding="utf-8")
    else:
        hook = hooks_dir / "pre-commit"
        hook.write_text(f'#!/bin/sh\necho HOOK > "{hook_ran}"\n', encoding="utf-8")
        hook.chmod(0o755)

    (wt / "new.txt").write_text("content\n", encoding="utf-8")
    runner = GitRunner(paths.bin)
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=wt,
        empty_hooks=paths.empty_hooks,
        args=["add", "new.txt"],
    )
    await task_git(
        runner,
        git_dir=git_dir,
        worktree=wt,
        empty_hooks=paths.empty_hooks,
        args=["commit", "-m", "add new.txt"],
    )
    assert not hook_ran.exists()


async def test_task_git_error_scrubbed_and_raised(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n"}, git_env)
    wt = tmp_path / "wt"
    _cmd(["git", "worktree", "add", "-b", "agent/test/task-err", str(wt)], cwd=repo)
    git_dir = git_dir_for_task(repo, "wt")

    async def token() -> str:
        return "secret_tok_999"

    runner = GitRunner(paths.bin, token)
    with pytest.raises(GitError) as exc_info:
        await task_git(
            runner,
            git_dir=git_dir,
            worktree=wt,
            empty_hooks=paths.empty_hooks,
            args=["fetch", "https://github.com/secret_tok_999/nonexistent.git"],
            authenticated=True,
        )
    assert "secret_tok_999" not in str(exc_info.value)
    assert exc_info.value.code != 0
