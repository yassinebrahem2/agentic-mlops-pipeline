from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import insert, select, update

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.agents.tasks import commit as commit_module
from mlops_agents.agents.tasks import service as service_module
from mlops_agents.agents.tasks.contract import CommitRefused, CommitResult
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims, integrations, sessions, tasks
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


def _cmd(
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


async def _setup_service(
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
    _cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    _cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)
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


async def _create_session(
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
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Add script")

    # Write a file directly into the worktree (Bash-like change)
    script_file = task.worktree / "script.py"
    script_file.write_text("print('bash like')\n", encoding="utf-8")

    result = await service.commit(project_id, task, "feat: add bash script")

    assert isinstance(result, CommitResult)
    assert len(result.sha) == 40
    assert result.changed == ("script.py",)

    # Verify commit on HEAD
    log = _cmd(["git", "log", "-1", "--pretty=format:%B"], cwd=task.worktree)
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
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Secret test")

    # Create secret file (.env)
    (task.worktree / ".env").write_text("API_KEY=12345\n", encoding="utf-8")

    with pytest.raises(CommitRefused, match=r"\.env"):
        await service.commit(project_id, task, "feat: add env")

    # Verify unstaged
    diff = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""

    # Verify .env was never staged or written into git object store
    hash_res = _cmd(["git", "hash-object", ".env"], cwd=task.worktree)
    blob_sha = hash_res.stdout.strip()
    cat_res = _cmd(["git", "cat-file", "-e", blob_sha], cwd=task.worktree, check=False)
    assert cat_res.returncode != 0


async def test_commit_failure_releases_claims_and_unstages(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    _cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    _cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)

    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Failed commit test")

    (task.worktree / "valid.py").write_text("x = 1\n", encoding="utf-8")

    # Mock task_git to simulate a failure specifically during git commit
    orig_task_git = commit_module.task_git

    async def mock_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            raise RuntimeError("Simulated failure during git commit")
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", mock_task_git)

    with pytest.raises(RuntimeError, match="Simulated failure during git commit"):
        await service.commit(project_id, task, "feat: will fail")

    # Verify nothing is staged
    diff = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""

    # Verify claims were released
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = (
            await conn.execute(
                select(file_claims).where(
                    file_claims.c.task_id == task.id, file_claims.c.path == "valid.py"
                )
            )
        ).all()
        assert len(claims) == 0


async def test_commit_claim_conflict_refused_and_unstaged(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="agent_one")
    await _create_session(databases, project_id, "ses_2", agent="agent_two")

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
    diff = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task2.worktree)
    assert diff.stdout.strip() == ""


async def test_commit_author_identity_human_and_ai_fallback(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # 1. Fallback to GitHub login when git config is unset
    service, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env, github_login="octocat"
    )
    # Unset user.name and user.email in repo config
    _cmd(["git", "config", "--unset", "user.name"], cwd=repo, check=False)
    _cmd(["git", "config", "--unset", "user.email"], cwd=repo, check=False)

    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Author fallback test")

    (task.worktree / "mod.py").write_text("a = 1\n", encoding="utf-8")
    await service.commit(project_id, task, "commit with login fallback")

    log = _cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
    assert log.stdout.strip() == "octocat <octocat@users.noreply.github.com>"

    # 2. Configured user.name and user.email are used
    _cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    _cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)

    (task.worktree / "mod2.py").write_text("b = 2\n", encoding="utf-8")
    await service.commit(project_id, task, "commit with user config")

    log2 = _cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
    assert log2.stdout.strip() == "Jane Doe <jane@example.com>"

    # 3. Inherited GIT_AUTHOR_* and GIT_COMMITTER_* env vars are cleared and ignored
    old_author = os.environ.get("GIT_AUTHOR_NAME")
    os.environ["GIT_AUTHOR_NAME"] = "Attacker Name"
    os.environ["GIT_COMMITTER_NAME"] = "Attacker Name"
    try:
        (task.worktree / "mod3.py").write_text("c = 3\n", encoding="utf-8")
        await service.commit(project_id, task, "commit with env override")
        log3 = _cmd(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=task.worktree)
        assert log3.stdout.strip() == "Jane Doe <jane@example.com>"
    finally:
        if old_author is None:
            os.environ.pop("GIT_AUTHOR_NAME", None)
            os.environ.pop("GIT_COMMITTER_NAME", None)
        else:
            os.environ["GIT_AUTHOR_NAME"] = old_author
            os.environ["GIT_COMMITTER_NAME"] = old_author

    # 4. Refuse commit when neither git config nor GitHub login exists (never invent 'user')
    _cmd(["git", "config", "--unset", "user.name"], cwd=repo)
    _cmd(["git", "config", "--unset", "user.email"], cwd=repo)
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(update(integrations).values(login=None))

    (task.worktree / "mod4.py").write_text("d = 4\n", encoding="utf-8")
    with pytest.raises(CommitRefused, match="Cannot determine commit author"):
        await service.commit(project_id, task, "commit with no identity")


async def test_staged_diff_and_cap(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Diff test")

    # Normal diff
    (task.worktree / "hello.txt").write_text("Hello World\n", encoding="utf-8")
    diff = await service.staged_diff(project_id, task)
    assert "+Hello World" in diff
    assert "diff --git" in diff

    # Calling staged_diff unstages changes
    diff_cached = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff_cached.stdout.strip() == ""

    # Secret files (.env) are excluded from staged_diff, state refusal plainly, and never enter object store
    (task.worktree / ".env").write_text("SUPER_SECRET_TOKEN=xyz\n", encoding="utf-8")
    (task.worktree / "public.py").write_text("public_var = 42\n", encoding="utf-8")
    diff_with_secret = await service.staged_diff(project_id, task)
    assert "+public_var = 42" in diff_with_secret
    assert "SUPER_SECRET_TOKEN" not in diff_with_secret
    assert "Commit will refuse: secret files present (.env)." in diff_with_secret

    hash_res = _cmd(["git", "hash-object", ".env"], cwd=task.worktree)
    blob_sha = hash_res.stdout.strip()
    cat_res = _cmd(["git", "cat-file", "-e", blob_sha], cwd=task.worktree, check=False)
    assert cat_res.returncode != 0

    # Claim conflict is stated plainly in staged_diff
    await _create_session(databases, project_id, "ses_other", agent="other_agent")
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
    _cmd(["git", "init", "--bare", str(bare)])
    seed = tmp_path / "seed"
    make_repo(seed, {"remote_file.txt": "remote\n"}, git_env)
    _cmd(["git", "push", str(bare), "main"], cwd=seed)

    # 2. Local client repo with origin
    service, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    _cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=repo,
    )

    # 3. Configure insteadOf via fake_global
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
        await _create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Push test")

        (task.worktree / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
        commit_res = await service.commit(project_id, task, "feat: commit to push")

        # Plant an existing user origin ref to prove push never deletes or mutates user origin refs
        _cmd(["git", "update-ref", f"refs/remotes/origin/{task.branch}", "HEAD"], cwd=repo)

        # Snapshot user repo config and origin refs before push
        config_before = (repo / ".git" / "config").read_bytes()
        refs_before = _cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout

        await service.push(project_id, task)

        # Assert user repo config and origin/* refs are byte-for-byte unchanged
        config_after = (repo / ".git" / "config").read_bytes()
        refs_after = _cmd(["git", "for-each-ref", "refs/remotes/origin"], cwd=repo).stdout
        assert config_before == config_after
        assert refs_before == refs_after

        # Verify pushed sha is recorded in refs/mlops-agents/pushed/<branch>
        pushed_ref_sha = _cmd(
            ["git", "rev-parse", f"refs/mlops-agents/pushed/{task.branch}"], cwd=repo
        ).stdout.strip()
        assert pushed_ref_sha == commit_res.sha

        # Verify bare repo has the branch and commit
        pushed_sha = _cmd(
            ["git", "rev-parse", f"refs/heads/{task.branch}"], cwd=bare
        ).stdout.strip()
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
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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
    _cmd(["git", "init", "--bare", str(bare)])
    seed2 = tmp_path / "seed2"
    make_repo(seed2, {"remote_file2.txt": "remote2\n"}, git_env)
    _cmd(["git", "push", str(bare), "main"], cwd=seed2)

    service2, _, _, prj2_id, repo2 = await _setup_service(
        tmp_path / "prj2", paths, databases, keystore, git_env
    )
    _cmd(
        ["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"],
        cwd=repo2,
    )

    fake_global = tmp_path / "fake_global2.gitconfig"
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
        await _create_session(databases, prj2_id, "ses_2", agent="coder")
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


class FakeSandbox:
    def __init__(self) -> None:
        self.paused_calls: list[tuple[str, str]] = []
        self.is_paused: bool = False

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None, None]:
        self.paused_calls.append((project_id, task_id))
        self.is_paused = True
        try:
            yield
        finally:
            self.is_paused = False


async def test_commit_and_staged_diff_pause_and_unpause_on_raise(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = FakeSandbox()
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env, sandbox=fake_sandbox
    )

    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Pause test")

    # 1. staged_diff enters and exits paused
    (task.worktree / "file.py").write_text("x = 1\n", encoding="utf-8")
    assert not fake_sandbox.is_paused
    diff = await service.staged_diff(project_id, task)
    assert diff
    assert not fake_sandbox.is_paused
    assert (project_id, task.id) in fake_sandbox.paused_calls

    # 2. staged_diff exits paused even when raising
    async def failing_diff(*args: Any, **kwargs: Any) -> Any:
        assert fake_sandbox.is_paused
        raise RuntimeError("diff crash")

    monkeypatch.setattr(service_module, "staged_diff_task", failing_diff)
    with pytest.raises(RuntimeError, match="diff crash"):
        await service.staged_diff(project_id, task)
    assert not fake_sandbox.is_paused

    # 3. commit enters and exits paused
    monkeypatch.undo()
    assert not fake_sandbox.is_paused
    await service.commit(project_id, task, "feat: commit pause test")
    assert not fake_sandbox.is_paused

    # 4. commit exits paused even when raising
    (task.worktree / "file2.py").write_text("y = 2\n", encoding="utf-8")

    async def failing_commit(*args: Any, **kwargs: Any) -> Any:
        assert fake_sandbox.is_paused
        raise RuntimeError("commit crash")

    monkeypatch.setattr(service_module, "commit_task", failing_commit)
    with pytest.raises(RuntimeError, match="commit crash"):
        await service.commit(project_id, task, "feat: will fail")
    assert not fake_sandbox.is_paused


async def test_commit_failure_releases_only_newly_claimed_paths(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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
    diff = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""


async def test_commit_cancellation_cleans_up_claims_and_unstages(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel test")

    await service.claim(project_id, task, ["earlier.py"])

    (task.worktree / "earlier.py").write_text("# edited\n", encoding="utf-8")
    (task.worktree / "new_file.py").write_text("# new\n", encoding="utf-8")

    orig_task_git = commit_module.task_git

    async def mock_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            raise asyncio.CancelledError()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", mock_task_git)

    with pytest.raises(asyncio.CancelledError):
        await service.commit(project_id, task, "feat: cancel commit")

    # Check database: earlier.py still claimed, new_file.py released
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
    diff = _cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""


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

    service, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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


async def test_commit_cancellation_runner_blocks_inside_commit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel blocked commit test")

    (task.worktree / "work.py").write_text("a = 1\n", encoding="utf-8")

    orig_task_git = commit_module.task_git
    in_commit = asyncio.Event()
    release_commit = asyncio.Event()

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            in_commit.set()
            await release_commit.wait()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", blocking_task_git)

    caller_task = asyncio.create_task(service.commit(project_id, task, "feat: blocked commit"))

    # Wait until git commit is executing/blocked
    await in_commit.wait()

    # Cancel the caller while runner is blocked inside git commit
    caller_task.cancel()

    # Release git commit so it finishes successfully
    release_commit.set()

    # The caller receives CancelledError
    with pytest.raises(asyncio.CancelledError):
        await caller_task

    # BUT: Because git commit finished successfully, the commit landed!
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = set(
            (await conn.execute(select(file_claims.c.path).where(file_claims.c.task_id == task.id)))
            .scalars()
            .all()
        )
    assert "work.py" in claims

    # Check git log: the commit exists on HEAD!
    log_res = _cmd(["git", "log", "-1", "--format=%s"], cwd=task.worktree)
    assert log_res.stdout.strip() == "feat: blocked commit"


async def test_user_file_tmp_notes_is_committed_and_never_deleted(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    from mlops_agents.agents.temp_files import make_temp_name

    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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


async def test_leftover_generated_temp_file_not_counted_as_unpublished_change(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    from mlops_agents.agents.temp_files import make_temp_name

    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")
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
