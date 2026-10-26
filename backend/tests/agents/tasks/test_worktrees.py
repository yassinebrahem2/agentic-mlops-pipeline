from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mlops_agents.agents.tasks.worktrees import (
    TaskFiles,
    create_worktree,
    task_branch_name,
    title_slug,
)
from mlops_agents.agents.tools.base import ToolFailure
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.workspace.models import Project
from tests.conftest import make_repo


def _cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True, text=True)


def _make_project(
    project_id: str,
    local_path: str,
    *,
    source: str = "local",
    github_full_name: str | None = None,
    default_branch: str = "main",
) -> Project:
    return Project(
        id=project_id,
        name="test-project",
        source="local" if source == "local" else "github",
        local_path=local_path,
        github_full_name=github_full_name,
        default_branch=default_branch,
        status="ok",
        status_detail=None,
        created_at="2026-10-24T00:00:00Z",
        last_opened_at=None,
    )


def test_title_slug_and_branch_name() -> None:
    assert title_slug("Add OAuth login with GitHub!!") == "add-oauth-login-with-github"
    assert title_slug("A" * 60) == "a" * 40
    assert title_slug("---!@#$---") == "task"

    branch = task_branch_name("coder", "tsk_01JAX7M5H6ZABCDEF123456789", "Add OAuth login!")
    assert branch == "agent/coder/add-oauth-login-01JAX7"


async def test_secret_stripping_on_worktree_creation(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    # 1. Tracked .env and config.pem in a repository
    repo = make_repo(
        tmp_path / "repo",
        {
            "main.py": "print('hello')\n",
            ".env": "SECRET_KEY=supersecret\n",
            "config.pem": "-----BEGIN CERTIFICATE-----\n...\n",
            "safe.txt": "safe\n",
        },
        git_env,
    )
    user_git_config_before = (repo / ".git" / "config").read_bytes()

    project = _make_project("prj_test123", str(repo))
    worktree_path = paths.worktree_path(project.id, "tsk_1234567890")
    runner = GitRunner(paths.bin)

    branch, base_ref, base_sha = await create_worktree(
        runner=runner,
        project=project,
        repo_root=repo,
        worktree_path=worktree_path,
        empty_hooks=paths.empty_hooks,
        task_id="tsk_1234567890",
        agent="coder",
        title="Secret stripping test",
    )

    assert branch == "agent/coder/secret-stripping-test-123456"
    assert base_ref == "main"
    assert len(base_sha) == 40

    # Worktree lacks both secret files
    assert not (worktree_path / ".env").exists()
    assert not (worktree_path / "config.pem").exists()
    assert (worktree_path / "main.py").exists()
    assert (worktree_path / "safe.txt").exists()

    # git status in worktree is clean
    git_dir = repo / ".git" / "worktrees" / "tsk_1234567890"
    env = {**os.environ, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(worktree_path)}
    status = _cmd(["git", "status", "--porcelain"], cwd=worktree_path, env=env)
    assert status.stdout.strip() == ""

    # User's .git/config is byte-for-byte unchanged
    user_git_config_after = (repo / ".git" / "config").read_bytes()
    assert user_git_config_before == user_git_config_after


async def test_fake_remote_instead_of_fetches_namespaced_base_ref(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    # 1. Bare remote repo
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _cmd(["git", "init", "--bare", str(bare)])

    # Push initial commit to bare repo
    seed = tmp_path / "seed"
    seed.mkdir()
    _cmd(["git", "init", "-b", "main"], cwd=seed)
    _cmd(["git", "config", "user.name", "test"], cwd=seed)
    _cmd(["git", "config", "user.email", "test@test.com"], cwd=seed)
    (seed / "remote_file.txt").write_text("from remote\n", encoding="utf-8")
    _cmd(["git", "add", "-A"], cwd=seed)
    _cmd(["git", "commit", "-m", "remote init"], cwd=seed)
    remote_sha = _cmd(["git", "rev-parse", "HEAD"], cwd=seed).stdout.strip()
    _cmd(["git", "push", str(bare), "main"], cwd=seed)

    # 2. Local client repo
    local = make_repo(tmp_path / "local", {"local.txt": "local\n"}, git_env)
    # Add fake origin remote pointing to github.com
    _cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=local,
    )

    # 3. Configure url.<file_url>.insteadOf in a gitconfig pointed to by GIT_CONFIG_GLOBAL
    fake_global = tmp_path / "fake_global.gitconfig"
    bare_url = bare.as_uri()
    _cmd(
        [
            "git",
            "config",
            "--file",
            str(fake_global),
            f"url.{bare_url}.insteadOf",
            "https://github.com/test-owner/test-repo.git",
        ]
    )
    old_global = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(fake_global)
    try:
        project = _make_project("prj_localwithremote", str(local))
        worktree_path = paths.worktree_path(project.id, "tsk_fake_remote_01")
        runner = GitRunner(paths.bin)

        branch, base_ref, base_sha = await create_worktree(
            runner=runner,
            project=project,
            repo_root=local,
            worktree_path=worktree_path,
            empty_hooks=paths.empty_hooks,
            task_id="tsk_fake_remote_01",
            agent="coder",
            title="Fake remote test",
        )

        assert branch == "agent/coder/fake-remote-test-fake_r"
        assert base_ref == "refs/mlops-agents/base/main"
        assert base_sha == remote_sha

        # origin/* refs are untouched
        origin_refs = _cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=local).stdout.strip()
        assert origin_refs == ""

        # Worktree has remote_file.txt from the fetched base
        assert (worktree_path / "remote_file.txt").exists()
    finally:
        if old_global is not None:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global
        else:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)


async def test_task_files_views_untracked_and_filters_secrets_and_ignored(
    tmp_path: Path, git_env: dict[str, str], paths: DataPaths
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        {
            "main.py": "print(1)\n",
            ".gitignore": "*.log\n",
        },
        git_env,
    )
    project = _make_project("prj_files", str(repo))
    worktree_path = paths.worktree_path(project.id, "tsk_files_123456")
    runner = GitRunner(paths.bin)

    await create_worktree(
        runner=runner,
        project=project,
        repo_root=repo,
        worktree_path=worktree_path,
        empty_hooks=paths.empty_hooks,
        task_id="tsk_files_123456",
        agent="coder",
        title="Files test",
    )

    git_dir = repo / ".git" / "worktrees" / "tsk_files_123456"
    task_files = TaskFiles(
        root=worktree_path,
        git_dir=git_dir,
        empty_hooks=paths.empty_hooks,
        runner=runner,
    )

    # Initial files: main.py and .gitignore
    files = await task_files.tracked()
    assert files == [".gitignore", "main.py"]

    # Add a new untracked file, an ignored file, and a secret file
    (worktree_path / "new_module.py").write_text("print(2)\n", encoding="utf-8")
    (worktree_path / "debug.log").write_text("debug\n", encoding="utf-8")
    (worktree_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (worktree_path / "secret.key").write_text("KEY\n", encoding="utf-8")

    # Before invalidate(), cached listing is returned
    assert await task_files.tracked() == [".gitignore", "main.py"]

    # After invalidate(), newly created file is visible, while ignored and secret files are omitted
    task_files.invalidate()
    new_files = await task_files.tracked()
    assert new_files == [".gitignore", "main.py", "new_module.py"]

    # resolve_file
    assert await task_files.resolve_file("new_module.py") == "new_module.py"
    with pytest.raises(ToolFailure, match="secret file"):
        await task_files.resolve_file(".env")

    with pytest.raises(ToolFailure, match="outside the repository"):
        await task_files.resolve_file("../outside.txt")

    with pytest.raises(ToolFailure, match="not a file"):
        await task_files.resolve_file("nonexistent.py")

    # resolve_dir
    assert await task_files.resolve_dir(".") == ""
    assert await task_files.resolve_dir(None) == ""
