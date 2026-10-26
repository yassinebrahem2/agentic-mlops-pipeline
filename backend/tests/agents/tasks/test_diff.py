from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.agents.tasks import diff as diff_module
from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.tasks.helpers import cmd, create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_staged_diff_and_cap(
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
    task = await service.start(project_id, "ses_1", "coder", "Diff test")

    # Normal diff
    (task.worktree / "hello.txt").write_text("Hello World\n", encoding="utf-8")
    diff = await service.staged_diff(project_id, task)
    assert "+Hello World" in diff
    assert "diff --git" in diff

    # Calling staged_diff unstages changes
    diff_cached = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff_cached.stdout.strip() == ""

    # Secret files (.env) are excluded from staged_diff, state refusal plainly, and never enter object store
    (task.worktree / ".env").write_text("SUPER_SECRET_TOKEN=xyz\n", encoding="utf-8")
    (task.worktree / "public.py").write_text("public_var = 42\n", encoding="utf-8")
    diff_with_secret = await service.staged_diff(project_id, task)
    assert "+public_var = 42" in diff_with_secret
    assert "SUPER_SECRET_TOKEN" not in diff_with_secret
    assert "Commit will refuse: secret files present (.env)." in diff_with_secret

    hash_res = cmd(["git", "hash-object", ".env"], cwd=task.worktree)
    blob_sha = hash_res.stdout.strip()
    cat_res = cmd(["git", "cat-file", "-e", blob_sha], cwd=task.worktree, check=False)
    assert cat_res.returncode != 0

    # Claim conflict is stated plainly in staged_diff
    await create_session(databases, project_id, "ses_other", agent="other_agent")
    task2 = await service.start(project_id, "ses_other", "other_agent", "Other task")
    await service.claim(project_id, task2, ["claimed.py"])
    (task.worktree / "claimed.py").write_text("import sys\n", encoding="utf-8")
    diff_with_conflict = await service.staged_diff(project_id, task)
    assert "Commit will refuse: files claimed by another task (claimed.py)." in diff_with_conflict

    # Large diff capped at 30,000 characters
    (task.worktree / "large.txt").write_text("x" * 70000 + "\n", encoding="utf-8")
    large_diff = await service.staged_diff(project_id, task)
    assert len(large_diff) <= 30000 + 100
    assert "[... diff cut:" in large_diff


async def test_temp_files_excluded_from_candidate_paths(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    from mlops_agents.agents.tasks.diff import get_candidate_paths
    from mlops_agents.agents.tasks.git import git_dir_for_task
    from mlops_agents.agents.temp_files import make_temp_name

    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Temp exclusion test")

    (task.worktree / "valid.py").write_text("x = 1\n", encoding="utf-8")
    (task.worktree / ".tmp_notes").write_text("notes\n", encoding="utf-8")
    gen_tmp = task.worktree / make_temp_name("valid.py")
    gen_tmp.write_text("temp data\n", encoding="utf-8")

    git_dir = git_dir_for_task(repo, task.id)
    runner = GitRunner(paths.bin)
    candidates = await get_candidate_paths(
        runner,
        git_dir=git_dir,
        worktree=task.worktree,
        empty_hooks=paths.empty_hooks,
    )
    assert "valid.py" in candidates
    assert ".tmp_notes" in candidates
    assert gen_tmp.name not in candidates


async def test_user_file_tmp_notes_is_committed_and_never_deleted(
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
    task = await service.start(project_id, "ses_1", "coder", "User temp file test")

    (task.worktree / ".tmp_notes").write_text("my notes\n", encoding="utf-8")
    generated_tmp = task.worktree / make_temp_name("auto.py")
    generated_tmp.write_text("leftover\n", encoding="utf-8")

    # Commit should commit .tmp_notes and exclude generated_tmp
    res = await service.commit(project_id, task, "feat: commit user tmp_notes")
    assert ".tmp_notes" in res.changed
    assert generated_tmp.name not in res.changed

    # Resume cleans generated temp files but preserves user .tmp_notes
    await service.resume(project_id, "ses_1", "coder", task.id)
    assert (task.worktree / ".tmp_notes").exists()
    assert not generated_tmp.exists()


async def test_literal_pathspecs_committed_literally(
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
    task = await service.start(project_id, "ses_1", "coder", "Literal pathspecs test")

    (task.worktree / "[a-z].txt").write_text("bracket file\n", encoding="utf-8")
    (task.worktree / "a.txt").write_text("plain file\n", encoding="utf-8")

    filenames_to_check = ["[a-z].txt"]

    if sys.platform != "win32":
        (task.worktree / ":(glob)*").write_text("magic glob file\n", encoding="utf-8")
        filenames_to_check.append(":(glob)*")

    diff = await service.staged_diff(project_id, task)
    assert "[a-z].txt" in diff
    if sys.platform != "win32":
        assert ":(glob)*" in diff

    res = await service.commit(project_id, task, "feat: commit literal filenames")
    for name in filenames_to_check:
        assert name in res.changed

    show_res = cmd(["git", "show", "--name-only", "--format=", res.sha], cwd=task.worktree)
    committed_files = show_res.stdout.splitlines()
    for name in filenames_to_check:
        assert name in committed_files


async def test_diff_failing_cleanup_reset_logs_warning_with_stderr(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Failing reset diff test")

    (task.worktree / "mod.py").write_text("x = 1\n", encoding="utf-8")

    orig_task_git = diff_module.task_git

    async def mock_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "reset" in git_args_raw:
            from mlops_agents.integrations.github.git import GitResult

            return GitResult(code=1, stdout="", stderr="fatal: mock diff reset error")
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(diff_module, "task_git", mock_task_git)

    with caplog.at_level(logging.WARNING):
        diff = await service.staged_diff(project_id, task)

    assert "+x = 1" in diff
    assert (
        "Failed to reset working tree during staged_diff cleanup (exit code 1): fatal: mock diff reset error"
        in caplog.text
    )


async def test_staged_diff_cancellation_during_add_executes_cleanup(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel diff add test")

    (task.worktree / "work.py").write_text("a = 100\n", encoding="utf-8")

    orig_task_git = diff_module.task_git
    in_add = asyncio.Event()
    release_add = asyncio.Event()
    add_completed = asyncio.Event()
    git_calls: list[str] = []

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence):
            if "add" in git_args_raw:
                git_calls.append("add")
                in_add.set()
                await release_add.wait()
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.05)
                res = await orig_task_git(*args, **kwargs)
                add_completed.set()
                return res
            if "reset" in git_args_raw:
                git_calls.append("reset")
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(diff_module, "task_git", blocking_task_git)

    caller_task = asyncio.create_task(service.staged_diff(project_id, task))
    await in_add.wait()
    caller_task.cancel()
    release_add.set()

    with pytest.raises(asyncio.CancelledError):
        await caller_task

    await add_completed.wait()

    assert "add" in git_calls
    assert "reset" in git_calls
    assert git_calls.index("reset") > git_calls.index("add")

    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""


async def test_literal_pathspecs_prevents_glob_matching_secret_env(
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
    task = await service.start(project_id, "ses_1", "coder", "Glob literal test")

    (task.worktree / ".env").write_text("SUPER_SECRET_TOKEN=xyz123\n", encoding="utf-8")
    (task.worktree / "[.]env").write_text("PUBLIC_CONFIG=visible\n", encoding="utf-8")

    diff = await service.staged_diff(project_id, task)
    assert "PUBLIC_CONFIG=visible" in diff
    assert "SUPER_SECRET_TOKEN" not in diff
    assert "xyz123" not in diff
    assert "Commit will refuse: secret files present (.env)." in diff

    # .env must not have entered git object store
    hash_res = cmd(["git", "hash-object", ".env"], cwd=task.worktree)
    blob_sha = hash_res.stdout.strip()
    cat_res = cmd(["git", "cat-file", "-e", blob_sha], cwd=task.worktree, check=False)
    assert cat_res.returncode != 0

    # Commit refuses while .env is present
    with pytest.raises(CommitRefused, match=r"\.env"):
        await service.commit(project_id, task, "refused commit")

    # Remove secret .env and commit [.]env
    (task.worktree / ".env").unlink()
    commit_res = await service.commit(project_id, task, "feat: commit literal bracket file")
    assert commit_res.changed == ("[.]env",)

    show_res = cmd(["git", "show", "--name-only", "--format=", commit_res.sha], cwd=task.worktree)
    files = show_res.stdout.splitlines()
    assert "[.]env" in files
    assert ".env" not in files
