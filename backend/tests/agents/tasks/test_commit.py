from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import insert, select, update

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.agents.tasks import commit as commit_module
from mlops_agents.agents.tasks.contract import CommitRefused, CommitResult
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims, integrations, sessions
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


def cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True, text=True)


class FakeEventLog(EventLog):
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        record = EventRecord(
            id=new_id("evt"),
            seq=len(self.records) + 1,
            ts=now_iso(),
            source=source,
            type=type,
            subject_id=subject_id,
            payload=payload,
        )
        self.records.append(
            {
                "project_id": project_id,
                "source": source,
                "type": type,
                "payload": payload,
                "subject_id": subject_id,
                "record": record,
            }
        )
        return record

    async def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        if False:
            yield

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        return [
            r["record"]
            for r in reversed(self.records)
            if r["project_id"] == project_id and r["type"] in types
        ][:limit]


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


async def setup_service(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    *,
    files: dict[str, str] | None = None,
    github_login: str | None = None,
    sandbox: Any = None,
) -> tuple[TasksService, WorkspaceService, FakeEventLog, str, Path]:
    repo = make_repo(tmp_path / "repo", files or {"app.py": "print('hello')\n"}, git_env)
    cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    if github_login:
        engine = await databases.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(integrations).values(
                    name="github",
                    mode="token",
                    login=github_login,
                    verified_at=now_iso(),
                )
            )
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(_settings(paths), paths, databases, runner, github, http)
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    service = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
        sandbox=sandbox,
    )
    return service, workspace, events, project.id, repo


async def create_session(
    databases: Databases, project_id: str, session_id: str, agent: str = "coder"
) -> None:
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                agent=agent,
                summary=None,
                compacted_through=None,
                created_at=now_iso(),
            )
        )


async def test_commit_bash_like_change(
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
    task = await service.start(project_id, "ses_1", "coder", "Add script")

    # Write a file directly into the worktree (Bash-like change)
    script_file = task.worktree / "script.py"
    script_file.write_text("print('bash like')\n", encoding="utf-8")

    result = await service.commit(project_id, task, "feat: add bash script")

    assert isinstance(result, CommitResult)
    assert len(result.sha) == 40
    assert result.changed == ("script.py",)

    # Verify commit on HEAD
    log = cmd(["git", "log", "-1", "--pretty=format:%B"], cwd=task.worktree)
    assert "feat: add bash script" in log.stdout

    # Verify claims recorded
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = (
            (await conn.execute(select(file_claims.c.path).where(file_claims.c.task_id == task.id)))
            .scalars()
            .all()
        )
    assert "script.py" in claims


async def test_commit_clean_worktree_refused(
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
    task = await service.start(project_id, "ses_1", "coder", "Clean commit")

    with pytest.raises(CommitRefused, match="working tree clean"):
        await service.commit(project_id, task, "feat: clean")


async def test_commit_secret_file_refused_and_unstaged(
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
    task = await service.start(project_id, "ses_1", "coder", "Secret test")

    (task.worktree / ".env").write_text("API_KEY=12345\n", encoding="utf-8")

    with pytest.raises(CommitRefused, match=r"\.env"):
        await service.commit(project_id, task, "feat: add env")

    # Verify unstaged
    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""

    # Verify .env was never staged or written into git object store
    hash_res = cmd(["git", "hash-object", ".env"], cwd=task.worktree)
    blob_sha = hash_res.stdout.strip()
    cat_res = cmd(["git", "cat-file", "-e", blob_sha], cwd=task.worktree, check=False)
    assert cat_res.returncode != 0


async def test_commit_claim_conflict_refused_and_unstaged(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="agent_one")
    await create_session(databases, project_id, "ses_2", agent="agent_two")

    task1 = await service.start(project_id, "ses_1", "agent_one", "Task 1")
    task2 = await service.start(project_id, "ses_2", "agent_two", "Task 2")

    # Task 1 claims shared.py
    await service.claim(project_id, task1, ["shared.py"])

    # Task 2 writes shared.py and attempts to commit
    (task2.worktree / "shared.py").write_text("# task 2\n", encoding="utf-8")

    with pytest.raises(CommitRefused) as exc_info:
        await service.commit(project_id, task2, "feat: edit shared")

    assert "shared.py" in str(exc_info.value)
    assert "agent_one" in str(exc_info.value)
    assert task1.id in str(exc_info.value)

    # Verify task 2's worktree has nothing staged
    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task2.worktree)
    assert diff.stdout.strip() == ""


async def test_commit_author_identity_human_and_ai_fallback(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # 1. Fallback to GitHub login when git config is unset
    service, _, _, project_id, repo = await setup_service(
        tmp_path, paths, databases, keystore, git_env, github_login="octocat"
    )
    cmd(["git", "config", "--unset", "user.name"], cwd=repo, check=False)
    cmd(["git", "config", "--unset", "user.email"], cwd=repo, check=False)

    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Author fallback test")

    (task.worktree / "mod.py").write_text("a = 1\n", encoding="utf-8")
    await service.commit(project_id, task, "commit with login fallback")

    log = cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
    assert log.stdout.strip() == "octocat <octocat@users.noreply.github.com>"

    # 2. Configured user.name and user.email are used
    cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)

    (task.worktree / "mod2.py").write_text("b = 2\n", encoding="utf-8")
    await service.commit(project_id, task, "commit with user config")

    log2 = cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
    assert log2.stdout.strip() == "Jane Doe <jane@example.com>"

    # 3. Inherited GIT_AUTHOR_* and GIT_COMMITTER_* env vars are cleared and ignored
    old_author = os.environ.get("GIT_AUTHOR_NAME")
    os.environ["GIT_AUTHOR_NAME"] = "Attacker Name"
    os.environ["GIT_COMMITTER_NAME"] = "Attacker Name"
    try:
        (task.worktree / "mod3.py").write_text("c = 3\n", encoding="utf-8")
        await service.commit(project_id, task, "commit with env override")
        log3 = cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
        assert log3.stdout.strip() == "Jane Doe <jane@example.com>"
    finally:
        if old_author is None:
            os.environ.pop("GIT_AUTHOR_NAME", None)
            os.environ.pop("GIT_COMMITTER_NAME", None)
        else:
            os.environ["GIT_AUTHOR_NAME"] = old_author
            os.environ["GIT_COMMITTER_NAME"] = old_author

    # 4. Refuse commit when neither git config nor GitHub login exists (never invent 'user')
    cmd(["git", "config", "--unset", "user.name"], cwd=repo)
    cmd(["git", "config", "--unset", "user.email"], cwd=repo)
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(update(integrations).values(login=None))

    (task.worktree / "mod4.py").write_text("d = 4\n", encoding="utf-8")
    with pytest.raises(CommitRefused, match="Cannot determine commit author"):
        await service.commit(project_id, task, "commit with no identity")


async def test_commit_failure_releases_only_newly_claimed_paths(
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
    task = await service.start(project_id, "ses_1", "coder", "Partial release test")

    # Claim earlier.py beforehand (simulating Write/Edit tool claim)
    await service.claim(project_id, task, ["earlier.py"])

    (task.worktree / "earlier.py").write_text("# edited earlier\n", encoding="utf-8")
    (task.worktree / "new_file.py").write_text("# newly added\n", encoding="utf-8")

    orig_task_git = commit_module.task_git

    async def mock_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            raise RuntimeError("Git commit failed unexpectedly")
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", mock_task_git)

    with pytest.raises(RuntimeError, match="Git commit failed unexpectedly"):
        await service.commit(project_id, task, "feat: fail commit")

    # Check database: earlier.py MUST STILL be claimed, new_file.py MUST NOT be claimed
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = set(
            (await conn.execute(select(file_claims.c.path).where(file_claims.c.task_id == task.id)))
            .scalars()
            .all()
        )
    assert "earlier.py" in claims
    assert "new_file.py" not in claims

    # Check git: nothing is staged
    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""
