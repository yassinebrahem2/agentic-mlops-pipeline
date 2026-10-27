"""Ending a task: a cleanup failure is logged, and the task still ends."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

from mlops_agents.agents.tasks.lifecycle import end_task
from mlops_agents.integrations.github.git import GitError, GitResult, GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.tasks.helpers import create_session, setup_service
from tests.conftest import MemoryKeyStore


class _FailingRemoveRunner(GitRunner):
    """Fails `git worktree remove` the way a missing git or a timeout does."""

    def __init__(self, bin_dir: Path) -> None:
        super().__init__(bin_dir)

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
        if "remove" in args:
            raise GitError("git worktree took longer than 600 seconds and was stopped.", code=-1)
        return await super().run(
            args,
            cwd,
            check=check,
            timeout=timeout,
            authenticated=authenticated,
            extra_env=extra_env,
        )


async def test_end_task_logs_a_failed_worktree_removal_and_still_ends(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, events, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_end_log")
    task = await service.start(project_id, "ses_end_log", "coder", "Ends anyway")

    with caplog.at_level(logging.WARNING):
        await end_task(
            runner=_FailingRemoveRunner(paths.bin),
            repo_root=repo,
            empty_hooks=paths.empty_hooks,
            databases=databases,
            project_id=project_id,
            task_id=task.id,
            status="discarded",
            events=events,
        )

    failures = [r for r in caplog.records if r.exc_info and isinstance(r.exc_info[1], GitError)]
    assert failures and str(task.worktree) in failures[0].getMessage()
    assert not task.worktree.exists()
    assert (await service.view(project_id, task.id)).status == "discarded"
    assert [r["type"] for r in events.records].count("task.ended") == 1
