from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

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


def remove_dir(path: Path) -> None:
    def on_exc(func: Any, target: str, exc: Any) -> None:
        Path(target).chmod(stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=on_exc)


@contextmanager
def git_instead_of(
    tmp_path: Path, bare: Path, remote_url: str = "https://github.com/test-owner/test-repo.git"
) -> Generator[None, None, None]:
    fake_global = tmp_path / f"fake_global_{bare.name}.gitconfig"
    cmd(["git", "config", "--file", str(fake_global), f"url.{bare.as_uri()}.insteadOf", remote_url])
    old_global = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(fake_global)
    try:
        yield
    finally:
        if old_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global


def make_bare_remote(tmp_path: Path, git_env: dict[str, str], name: str = "remote.git") -> Path:
    bare = tmp_path / name
    bare.mkdir(exist_ok=True)
    cmd(["git", "init", "--bare", str(bare)])
    seed = tmp_path / f"seed_{name}"
    make_repo(seed, {"remote_file.txt": "remote\n"}, git_env)
    cmd(["git", "push", str(bare), "main"], cwd=seed)
    return bare


async def test_push_to_fake_remote(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Push test")

        (task.worktree / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
        commit_res = await service.commit(project_id, task, "feat: commit to push")

        # Plant an existing user origin ref to prove push never deletes or mutates user origin refs
        cmd(["git", "update-ref", f"refs/remotes/origin/{task.branch}", "HEAD"], cwd=repo)

        config_before = (repo / ".git" / "config").read_bytes()
        refs_before = cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout

        await service.push(project_id, task)

        assert config_before == (repo / ".git" / "config").read_bytes()
        assert refs_before == cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout

        pushed_ref_sha = cmd(
            ["git", "rev-parse", f"refs/mlops-agents/pushed/{task.branch}"], cwd=repo
        ).stdout.strip()
        assert pushed_ref_sha == commit_res.sha

        pushed_sha = cmd(["git", "rev-parse", f"refs/heads/{task.branch}"], cwd=bare).stdout.strip()
        assert pushed_sha == commit_res.sha


async def test_push_without_remote_raises_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
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

    assert await service.has_unpublished_changes(project_id, task) is False

    (task.worktree / "change.txt").write_text("dirty\n", encoding="utf-8")
    assert await service.has_unpublished_changes(project_id, task) is True

    await service.commit(project_id, task, "feat: change")
    assert await service.has_unpublished_changes(project_id, task) is True

    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(status="in_review", review_kind="local")
        )
    assert await service.has_unpublished_changes(project_id, task) is False

    # --- Case 2: Project with RepoRef (remote) ---
    bare = make_bare_remote(tmp_path, git_env, name="remote2.git")
    service2, _, _, prj2_id, repo2 = await setup_service(
        tmp_path / "prj2", paths, databases, keystore, git_env
    )
    cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo2
    )

    with git_instead_of(tmp_path, bare):
        await create_session(databases, prj2_id, "ses_2", agent="coder")
        task2 = await service2.start(prj2_id, "ses_2", "coder", "Remote task")

        assert await service2.has_unpublished_changes(prj2_id, task2) is False

        (task2.worktree / "remote_change.txt").write_text("dirty\n", encoding="utf-8")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True

        await service2.commit(prj2_id, task2, "feat: remote change")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True

        await service2.push(prj2_id, task2)
        assert await service2.has_unpublished_changes(prj2_id, task2) is False

        (task2.worktree / "more.txt").write_text("more\n", encoding="utf-8")
        await service2.commit(prj2_id, task2, "feat: more change")
        assert await service2.has_unpublished_changes(prj2_id, task2) is True


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

    assert not await service.has_unpublished_changes(project_id, task)

    generated_tmp = task.worktree / make_temp_name("file.txt")
    generated_tmp.write_text("temp", encoding="utf-8")
    assert not await service.has_unpublished_changes(project_id, task)

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
    bare = make_bare_remote(tmp_path, git_env, name="remote_moving.git")
    service, _, _, project_id, repo = await setup_service(
        tmp_path / "client", paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Moving branch push test")

        (task.worktree / "first.txt").write_text("first commit\n", encoding="utf-8")
        c1 = await service.commit(project_id, task, "feat: first commit")

        orig_run_safe_git = push_module.run_safe_git

        async def moving_run_safe_git(*args: Any, **kwargs: Any) -> Any:
            git_args_raw = kwargs.get("args")
            if isinstance(git_args_raw, Sequence) and "push" in git_args_raw:
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

        remote_sha = cmd(["git", "rev-parse", f"refs/heads/{task.branch}"], cwd=bare).stdout.strip()
        assert remote_sha == c1.sha

        pushed_ref = f"refs/{APP_NAME}/pushed/{task.branch}"
        pushed_sha = cmd(["git", "rev-parse", pushed_ref], cwd=task.worktree).stdout.strip()
        assert pushed_sha == c1.sha

        head_sha = cmd(["git", "rev-parse", "HEAD"], cwd=task.worktree).stdout.strip()
        assert head_sha != pushed_sha

        assert await service.has_unpublished_changes(project_id, task) is True


@pytest.mark.parametrize("scenario", ["success", "failure", "cancellation"])
async def test_push_temp_dir_cleaned_up(
    scenario: str,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = make_bare_remote(tmp_path, git_env, name=f"remote_clean_{scenario}.git")
    service, _, _, project_id, repo = await setup_service(
        tmp_path / f"prj_{scenario}", paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", f"Push cleanup {scenario}")
        (task.worktree / "test.txt").write_text("content\n", encoding="utf-8")
        await service.commit(project_id, task, "feat: commit")

        captured_temp_dirs: list[Path] = []
        temp_dir_existed_on_push_return: list[bool] = []
        orig_run_safe_git = push_module.run_safe_git
        in_push = asyncio.Event()
        release_push = asyncio.Event()

        async def spy_run_safe_git(*args: Any, **kwargs: Any) -> Any:
            git_args_raw = kwargs.get("args")
            if isinstance(git_args_raw, Sequence):
                git_args = cast(Sequence[str], git_args_raw)
                if "init" in git_args:
                    captured_temp_dirs.append(Path(git_args[2]))
                elif "push" in git_args:
                    if scenario == "failure":
                        raise DomainError("mock_push_fail", "Push network failure")
                    if scenario == "cancellation":
                        in_push.set()
                        await release_push.wait()
                        res = await orig_run_safe_git(*args, **kwargs)
                        temp_dir_existed_on_push_return.append(captured_temp_dirs[0].exists())
                        return res
            return await orig_run_safe_git(*args, **kwargs)

        monkeypatch.setattr(push_module, "run_safe_git", spy_run_safe_git)

        if scenario == "success":
            await service.push(project_id, task)
        elif scenario == "failure":
            with pytest.raises(DomainError, match="Push network failure"):
                await service.push(project_id, task)
        elif scenario == "cancellation":
            push_job = asyncio.create_task(service.push(project_id, task))
            await in_push.wait()
            push_job.cancel()
            release_push.set()
            with pytest.raises(asyncio.CancelledError):
                await push_job
            assert temp_dir_existed_on_push_return == [True]

        assert len(captured_temp_dirs) == 1
        assert not captured_temp_dirs[0].exists()
        assert list(paths.empty_hooks.parent.glob(".tmp_push_*")) == []


async def test_has_unpublished_changes_when_worktree_deleted(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    bare = make_bare_remote(tmp_path, git_env, name="remote_del_wt.git")
    service, _, _, project_id, repo = await setup_service(
        tmp_path / "prj_del_wt", paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Deleted worktree test")
        (task.worktree / "file.txt").write_text("content\n", encoding="utf-8")
        await service.commit(project_id, task, "feat: commit before worktree removal")

        remove_dir(task.worktree)
        assert not task.worktree.exists()

        assert await service.has_unpublished_changes(project_id, task) is True

        await service.push(project_id, task)
        assert await service.has_unpublished_changes(project_id, task) is False


async def test_repo_ref_reads_origin_and_is_none_without_a_remote(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    assert await service.repo_ref(project_id) is None

    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)
    ref = await service.repo_ref(project_id)
    assert ref is not None
    assert (ref.owner, ref.repo) == ("test-owner", "test-repo")


async def test_has_commits_beyond_base_without_a_remote(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Beyond base")
    assert await service.has_commits_beyond_base(project_id, task) is False

    (task.worktree / "a.txt").write_text("a\n", encoding="utf-8")
    assert await service.has_commits_beyond_base(project_id, task) is False

    await service.commit(project_id, task, "feat: a")
    assert await service.has_commits_beyond_base(project_id, task) is True


async def test_has_commits_beyond_base_with_a_remote(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Remote beyond base")
        assert await service.has_commits_beyond_base(project_id, task) is False

        (task.worktree / "b.txt").write_text("b\n", encoding="utf-8")
        await service.commit(project_id, task, "feat: b")
        assert await service.has_commits_beyond_base(project_id, task) is True


async def test_has_commits_beyond_base_is_false_when_the_base_commit_is_gone(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Gone base")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task.id).values(base_sha="0" * 40))

    # The PR tool asks this while recovering from a refused commit; it must not raise.
    with caplog.at_level("WARNING", logger=push_module.logger.name):
        assert await service.has_commits_beyond_base(project_id, task) is False
    # The failure is not swallowed silently: it is logged with the task, the ref and git's reason.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(task.id in w and "0" * 40 in w and "Invalid revision range" in w for w in warnings)


async def _push_with_init_hook(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    after_init: Callable[[Path], None],
) -> list[Path]:
    """Push a committed task; `after_init(temp_push)` runs once the temp bare repo exists."""
    bare = make_bare_remote(tmp_path, git_env, name=f"remote_{name}.git")
    service, _, _, project_id, repo = await setup_service(
        tmp_path / f"prj_{name}", paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)
    temp_dirs: list[Path] = []
    orig_run_safe_git = push_module.run_safe_git

    async def spy_run_safe_git(*args: Any, **kwargs: Any) -> Any:
        res = await orig_run_safe_git(*args, **kwargs)
        git_args = cast(Sequence[str], kwargs.get("args") or [])
        if "init" in git_args:
            temp_dirs.append(Path(git_args[2]))
            after_init(temp_dirs[0])
        return res

    monkeypatch.setattr(push_module, "run_safe_git", spy_run_safe_git)
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", f"Push {name}")
        (task.worktree / "test.txt").write_text("content\n", encoding="utf-8")
        await service.commit(project_id, task, "feat: commit")
        await service.push(project_id, task)
    return temp_dirs


async def test_push_removes_a_read_only_file_in_its_temp_folder(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Git marks pack files read-only; Windows refuses to delete them unless the flag is cleared.
    def add_read_only_file(temp_push: Path) -> None:
        pack = temp_push / "objects" / "pack" / "pack-test.pack"
        pack.parent.mkdir(parents=True, exist_ok=True)
        pack.write_bytes(b"pack")
        pack.chmod(stat.S_IREAD)

    temp_dirs = await _push_with_init_hook(
        tmp_path, paths, databases, keystore, git_env, monkeypatch, "ro", add_read_only_file
    )

    assert len(temp_dirs) == 1
    assert not temp_dirs[0].exists()


async def test_push_logs_a_failed_cleanup_of_its_temp_folder(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_rmtree(path: Path) -> None:
        raise PermissionError(f"locked: {path}")

    monkeypatch.setattr(push_module, "rmtree", failing_rmtree)

    def nothing(_: Path) -> None:
        return None

    with caplog.at_level(logging.ERROR, logger="mlops_agents.agents.tasks.push"):
        temp_dirs = await _push_with_init_hook(
            tmp_path, paths, databases, keystore, git_env, monkeypatch, "fail", nothing
        )

    assert len(temp_dirs) == 1
    failures = [
        r
        for r in caplog.records
        if r.name == "mlops_agents.agents.tasks.push" and r.levelno >= logging.ERROR
    ]
    assert len(failures) == 1
    assert str(temp_dirs[0]) in failures[0].getMessage()
    assert failures[0].exc_info is not None
    remove_dir(temp_dirs[0])
