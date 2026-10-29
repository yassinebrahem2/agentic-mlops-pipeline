"""A cancelled or failing local merge cleans up after itself and leaves the user's repo as it was."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from mlops_agents.agents.tasks import local_merge as local_merge_module
from mlops_agents.agents.tasks.local_merge import local_merge
from mlops_agents.integrations.github.git import GitResult, GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.tasks.helpers import create_session, setup_service
from tests.conftest import MemoryKeyStore


def _is_merge(args: Sequence[str]) -> bool:
    return "--no-ff" in args


def _is_worktree_add(args: Sequence[str]) -> bool:
    return "worktree" in args and "add" in args


class _HeldRunner(GitRunner):
    """Holds the git call `held` matches until released. Like git in a worker thread, the held
    call keeps going when its caller is cancelled. Optionally fails the cleanup's worktree remove."""

    def __init__(
        self,
        bin_dir: Path,
        held: Callable[[Sequence[str]], bool],
        *,
        fail_remove: bool = False,
    ) -> None:
        super().__init__(bin_dir)
        self._held = held
        self._fail_remove = fail_remove
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        *,
        check: bool = True,
        timeout: float = 600,  # noqa: ASYNC109 -- mirrors GitRunner.run
        authenticated: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> GitResult:
        async def real() -> GitResult:
            return await super(_HeldRunner, self).run(
                args,
                cwd,
                check=check,
                timeout=timeout,
                authenticated=authenticated,
                extra_env=extra_env,
            )

        if self._held(args):
            self.started.set()

            async def held() -> GitResult:
                await self.release.wait()
                return await real()

            return await asyncio.shield(held())
        if self._fail_remove and "remove" in args:
            return GitResult(1, "", "fatal: simulated remove failure")
        return await real()


async def _git_out(args: list[str], cwd: Path, paths: DataPaths) -> str:
    return (await GitRunner(paths.bin).run(args, cwd=cwd)).stdout


async def _task_branch_with_a_commit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> tuple[str, str, Path]:
    """A task whose branch changes app.py; returns its id, its branch and the user's repo."""
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_merge")
    task = await service.start(project_id, "ses_merge", "coder", "Merge me")
    (task.worktree / "app.py").write_text("print('from task')\n", encoding="utf-8")
    await service.commit(project_id, task, "Change app")
    return task.id, task.branch, repo


@pytest.mark.parametrize("held", [_is_worktree_add, _is_merge], ids=["worktree-add", "merge"])
async def test_local_merge_cancelled_mid_git_call_leaves_no_temp_worktree_and_repo_untouched(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    held: Callable[[Sequence[str]], bool],
) -> None:
    task_id, branch, repo = await _task_branch_with_a_commit(
        tmp_path, paths, databases, keystore, git_env
    )
    main_before = await _git_out(["rev-parse", "refs/heads/main"], repo, paths)
    temp_base = tmp_path / "temp_merges"
    runner = _HeldRunner(paths.bin, held)

    merge = asyncio.create_task(
        local_merge(
            databases=databases,
            runner=runner,
            repo_root=repo,
            empty_hooks=paths.empty_hooks,
            temp_base_dir=temp_base,
            default_branch="main",
            task_branch=branch,
            task_id=task_id,
        )
    )
    await runner.started.wait()
    merge.cancel()

    # The cleanup must not race the git process still running in the temp worktree.
    done, _ = await asyncio.wait({merge}, timeout=1)
    assert not done
    runner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await merge

    assert list(temp_base.iterdir()) == []
    listing = await _git_out(["worktree", "list", "--porcelain"], repo, paths)
    assert "temp_merges" not in listing
    assert await _git_out(["rev-parse", "refs/heads/main"], repo, paths) == main_before
    assert await _git_out(["status", "--porcelain"], repo, paths) == ""
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('hello')\n"


async def test_local_merge_logs_a_failed_temp_worktree_removal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    task_id, branch, repo = await _task_branch_with_a_commit(
        tmp_path, paths, databases, keystore, git_env
    )
    runner = _HeldRunner(paths.bin, _is_merge, fail_remove=True)
    runner.release.set()

    with caplog.at_level(logging.WARNING):
        await local_merge(
            databases=databases,
            runner=runner,
            repo_root=repo,
            empty_hooks=paths.empty_hooks,
            temp_base_dir=tmp_path / "temp_merges",
            default_branch="main",
            task_branch=branch,
            task_id=task_id,
        )

    assert any("simulated remove failure" in r.getMessage() for r in caplog.records)


async def test_local_merge_cancelled_during_mkdtemp_leaves_no_temp_folder(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, branch, repo = await _task_branch_with_a_commit(
        tmp_path, paths, databases, keystore, git_env
    )
    temp_base = tmp_path / "temp_merges"
    created = threading.Event()
    release = threading.Event()

    def held_mkdtemp(prefix: str, dir: Path) -> str:
        # Like the real call in its worker thread, it goes on when its caller is cancelled.
        path = tempfile.mkdtemp(prefix=prefix, dir=dir)
        created.set()
        release.wait(timeout=10)
        return path

    monkeypatch.setattr(local_merge_module, "tempfile", SimpleNamespace(mkdtemp=held_mkdtemp))

    merge = asyncio.create_task(
        local_merge(
            databases=databases,
            runner=GitRunner(paths.bin),
            repo_root=repo,
            empty_hooks=paths.empty_hooks,
            temp_base_dir=temp_base,
            default_branch="main",
            task_branch=branch,
            task_id=task_id,
        )
    )
    await asyncio.to_thread(created.wait, 10)
    assert len(list(temp_base.iterdir())) == 1
    merge.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await merge

    assert list(temp_base.iterdir()) == []
