from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from mlops_agents.agents.tasks import push as push_module
from mlops_agents.config import APP_NAME
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tasks
from tests.agents.tasks.helpers import cmd, create_session, setup_service
from tests.conftest import MemoryKeyStore, make_repo


async def test_push_to_fake_remote(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # 1. Bare remote repo seeded with main
    bare = tmp_path / "remote.git"
    bare.mkdir()
    cmd(["git", "init", "--bare", str(bare)])
    seed = tmp_path / "seed"
    make_repo(seed, {"remote_file.txt": "remote\n"}, git_env)
    cmd(["git", "push", str(bare), "main"], cwd=seed)

    # 2. Local client repo with origin
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=repo,
    )

    # 3. Configure insteadOf via fake_global
    fake_global = tmp_path / "fake_global.gitconfig"
    bare_url = bare.as_uri()
    cmd(
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
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Push test")

        (task.worktree / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
        commit_res = await service.commit(project_id, task, "feat: commit to push")

        # Plant an existing user origin ref to prove push never deletes or mutates user origin refs
        cmd(["git", "update-ref", f"refs/remotes/origin/{task.branch}", "HEAD"], cwd=repo)

        # Snapshot user repo config and origin refs before push
        config_before = (repo / ".git" / "config").read_bytes()
        refs_before = cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout

        await service.push(project_id, task)

        # Assert user repo config and origin/* refs are byte-for-byte unchanged
        config_after = (repo / ".git" / "config").read_bytes()
        refs_after = cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout
        assert config_before == config_after
        assert refs_before == refs_after

        # Verify pushed sha is recorded in refs/mlops-agents/pushed/<branch>
        pushed_ref_sha = cmd(
            ["git", "rev-parse", f"refs/mlops-agents/pushed/{task.branch}"], cwd=repo
        ).stdout.strip()
        assert pushed_ref_sha == commit_res.sha

        # Verify bare repo has the branch and commit
        pushed_sha = cmd(["git", "rev-parse", f"refs/heads/{task.branch}"], cwd=bare).stdout.strip()
        assert pushed_sha == commit_res.sha
    finally:
        if old_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global


async def test_push_without_remote_raises_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # Local project without GitHub origin
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "No remote push")

    (task.worktree / "file.txt").write_text("hello\n", encoding="utf-8")
    await service.commit(project_id, task, "feat: some change")

    with pytest.raises(DomainError) as exc_info:
        await service.push(project_id, task)
    assert exc_info.value.code == "no_remote"


async def test_has_unpublished_changes(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # --- Case 1: Local project without RepoRef ---
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Local task")

    # 1. Clean worktree, just started -> False
    assert await service.has_unpublished_changes(project_id, task) is False

    # 2. Dirty worktree -> True
    (task.worktree / "change.txt").write_text("dirty\n", encoding="utf-8")
    assert await service.has_unpublished_changes(project_id, task) is True

    # 3. Committed -> ahead of base_sha, no review -> True
    await service.commit(project_id, task, "feat: change")
    assert await service.has_unpublished_changes(project_id, task) is True

    # 4. Local review exists -> False
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(status="in_review", review_kind="local")
        )
    assert await service.has_unpublished_changes(project_id, task) is False

    # --- Case 2: Project with RepoRef (remote) ---
    bare = tmp_path / "remote2.git"
    bare.mkdir()
    cmd(["git", "init", "--bare", str(bare)])
    seed2 = tmp_path / "seed2"
    make_repo(seed2, {"remote_file2.txt": "remote2\n"}, git_env)
    cmd(["git", "push", str(bare), "main"], cwd=seed2)

    service2, _, _, prj2_id, repo2 = await setup_service(
        tmp_path / "prj2", paths, databases, keystore, git_env
    )
    cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=repo2,
    )

    fake_global = tmp_path / "fake_global2.gitconfig"
    bare_url = bare.as_uri()
    cmd(
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
        await create_session(databases, prj2_id, "ses_2", agent="coder")
        task2 = await service2.start(prj2_id, "ses_2", "coder", "Remote task")

        # Clean worktree, just started -> False
        assert await service2.has_unpublished_changes(prj2_id, task2) is False

        # Dirty worktree -> True
        (task2.worktree / "remote_change.txt").write_text("dirty\n", encoding="utf-8")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True

        # Committed -> clean worktree, unpushed commits -> True
        await service2.commit(prj2_id, task2, "feat: remote change")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True

        # Pushed -> False
        await service2.push(prj2_id, task2)
        assert await service2.has_unpublished_changes(prj2_id, task2) is False

        # Another commit -> True
        (task2.worktree / "more.txt").write_text("more\n", encoding="utf-8")
        await service2.commit(prj2_id, task2, "feat: more change")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True
    finally:
        if old_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global


async def test_leftover_generated_temp_file_not_counted_as_unpublished_change(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    from mlops_agents.agents.temp_files import make_temp_name

    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Unpublished temp test")

    # Initially no unpublished changes
    assert not await service.has_unpublished_changes(project_id, task)

    # Creating a leftover generated temp file must NOT count as an unpublished change
    generated_tmp = task.worktree / make_temp_name("file.txt")
    generated_tmp.write_text("temp", encoding="utf-8")
    assert not await service.has_unpublished_changes(project_id, task)

    # But creating a real user file DOES count as unpublished change
    (task.worktree / "real.py").write_text("x = 1\n", encoding="utf-8")
    assert await service.has_unpublished_changes(project_id, task)


async def test_push_records_exact_pushed_sha_when_branch_moves_during_push(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = tmp_path / "remote_moving.git"
    bare.mkdir()
    cmd(["git", "init", "--bare", str(bare)])
    seed = tmp_path / "seed_moving"
    make_repo(seed, {"seed.txt": "seed\n"}, git_env)
    cmd(["git", "push", str(bare), "main"], cwd=seed)

    service, _, _, project_id, repo = await setup_service(
        tmp_path / "client", paths, databases, keystore, git_env
    )
    cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=repo,
    )

    fake_global = tmp_path / "fake_global_moving.gitconfig"
    bare_url = bare.as_uri()
    cmd(
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
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Moving branch push test")

        (task.worktree / "first.txt").write_text("first commit\n", encoding="utf-8")
        c1 = await service.commit(project_id, task, "feat: first commit")

        orig_run_safe_git = push_module.run_safe_git

        async def moving_run_safe_git(*args: Any, **kwargs: Any) -> Any:
            git_args_raw = kwargs.get("args")
            if isinstance(git_args_raw, Sequence) and "push" in git_args_raw:
                # Branch moves right before/during the push network command
                (task.worktree / "second.txt").write_text("second commit\n", encoding="utf-8")
                cmd(["git", "add", "second.txt"], cwd=task.worktree)
                cmd(
                    [
                        "git",
                        "-c",
                        "user.name=Jane Doe",
                        "-c",
                        "user.email=jane@example.com",
                        "commit",
                        "-m",
                        "feat: second commit",
                    ],
                    cwd=task.worktree,
                )
            return await orig_run_safe_git(*args, **kwargs)

        monkeypatch.setattr(push_module, "run_safe_git", moving_run_safe_git)

        await service.push(project_id, task)

        # The bare remote received c1.sha (the resolved sha pushed)
        remote_sha = cmd(["git", "rev-parse", f"refs/heads/{task.branch}"], cwd=bare).stdout.strip()
        assert remote_sha == c1.sha

        # The local pushed ref is recorded as c1.sha (NOT the new HEAD of the branch)
        pushed_ref = f"refs/{APP_NAME}/pushed/{task.branch}"
        pushed_sha = cmd(["git", "rev-parse", pushed_ref], cwd=task.worktree).stdout.strip()
        assert pushed_sha == c1.sha

        # New HEAD is different from pushed_sha
        head_sha = cmd(["git", "rev-parse", "HEAD"], cwd=task.worktree).stdout.strip()
        assert head_sha != pushed_sha

        # Because second commit is on the branch but wasn't pushed, has_unpublished_changes is True!
        assert await service.has_unpublished_changes(project_id, task) is True
    finally:
        if old_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global
