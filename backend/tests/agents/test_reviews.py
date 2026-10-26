from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from mlops_agents.agents.constants import cap_diff
from mlops_agents.agents.reviews import (
    decide_local_review,
    get_github_activity,
    list_local_reviews,
)
from mlops_agents.agents.runtime.contract import AgentSessions, MessageView
from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.integrations.github.git import GitRunner
from tests.api.conftest import add_local_project, build_app


@pytest.fixture(autouse=True)
def _suppress_docker_cleanup() -> Any:
    with patch(
        "mlops_agents.sandbox.containers.ContainerManager.cleanup_all",
        new=AsyncMock(),
    ):
        yield


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    home = tmp_path / "home"
    application = build_app(home)
    async with application.router.lifespan_context(application):
        yield application


async def _setup_project(app: FastAPI, tmp_path: Path, git_env: dict[str, str]) -> str:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    project = await services.workspace.get(project_id)
    repo_root = services.workspace.repo_root(project)
    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    await run_safe_git(
        git,
        args=["config", "user.name", "Test User"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )
    await run_safe_git(
        git,
        args=["config", "user.email", "test@example.com"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )
    return project_id


async def test_cap_diff_helper() -> None:
    short_diff = "diff --git a/file b/file\n+line"
    assert cap_diff(short_diff) == short_diff

    long_diff = "a" * 35000
    capped = cap_diff(long_diff)
    assert len(capped) < 35000
    assert "omitted" in capped


async def test_list_local_reviews_and_filtering(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")

    # Task 1: local review
    task1 = await services.tasks.start(project_id, session.id, "orchestrator", "Task 1 Local")
    file1 = task1.worktree / "new_file.txt"
    file1.write_text("Hello from task 1\n", encoding="utf-8")
    await services.tasks.claim(project_id, task1, ["new_file.txt"])
    await services.tasks.commit(project_id, task1, "Add new_file.txt")
    await services.tasks.set_review(project_id, task1.id, kind="local")

    # Task 2: github review
    task2 = await services.tasks.start(project_id, session.id, "orchestrator", "Task 2 GitHub")
    file2 = task2.worktree / "gh_file.txt"
    file2.write_text("Hello from task 2\n", encoding="utf-8")
    await services.tasks.claim(project_id, task2, ["gh_file.txt"])
    await services.tasks.commit(project_id, task2, "Add gh_file.txt")
    await services.tasks.set_review(
        project_id, task2.id, kind="github", pr_number=42, pr_url="https://github.com/pr/42"
    )

    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    reviews = await list_local_reviews(
        tasks=services.tasks,
        workspace=services.workspace,
        git=git,
        empty_hooks=services.paths.empty_hooks,
        project_id=project_id,
    )

    assert len(reviews) == 1
    review = reviews[0]
    assert review.task.id == task1.id
    assert review.agent == "orchestrator"
    assert "new_file.txt" in review.files_changed
    assert "new_file.txt" in review.diff
    assert "+Hello from task 1" in review.diff


async def test_decide_local_review_approve_clean(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")

    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task to Approve")
    feature_file = task.worktree / "feature.py"
    feature_file.write_text("print('feature')\n", encoding="utf-8")
    await services.tasks.claim(project_id, task, ["feature.py"])
    await services.tasks.commit(project_id, task, "feat: add feature")
    await services.tasks.set_review(project_id, task.id, kind="local")

    updated_task = await decide_local_review(
        tasks=services.tasks,
        sessions=services.sessions,
        project_id=project_id,
        task_id=task.id,
        approve=True,
    )

    assert updated_task.status == "merged"

    # Verify merged file exists in project repo
    project = await services.workspace.get(project_id)
    repo_root = services.workspace.repo_root(project)
    assert (repo_root / "feature.py").exists()


async def test_decide_local_review_approve_conflict(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    project = await services.workspace.get(project_id)
    repo_root = services.workspace.repo_root(project)
    session = await services.sessions.get(project_id, "orchestrator")

    # Start task and modify README.md
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task with Conflict")
    task_readme = task.worktree / "README.md"
    task_readme.write_text("# Task Version\n", encoding="utf-8")
    await services.tasks.claim(project_id, task, ["README.md"])
    await services.tasks.commit(project_id, task, "change readme in task")
    await services.tasks.set_review(project_id, task.id, kind="local")

    # Now modify README.md on main branch in repo_root to introduce conflict
    main_readme = repo_root / "README.md"
    main_readme.write_text("# Conflicting Main Version\n", encoding="utf-8")
    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    await run_safe_git(
        git,
        args=["commit", "-am", "conflicting commit on main"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )

    with pytest.raises(DomainError) as exc_info:
        await decide_local_review(
            tasks=services.tasks,
            sessions=services.sessions,
            project_id=project_id,
            task_id=task.id,
            approve=True,
        )

    assert exc_info.value.code == "conflict"
    assert exc_info.value.status == 409

    # Verify task remains in_review
    task_in_db = await services.tasks.view(project_id, task.id)
    assert task_in_db.status == "in_review"


async def test_decide_local_review_reject(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")

    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task to Reject")
    await services.tasks.set_review(project_id, task.id, kind="local")

    updated_task = await decide_local_review(
        tasks=services.tasks,
        sessions=services.sessions,
        project_id=project_id,
        task_id=task.id,
        approve=False,
        note="Needs better formatting",
    )

    assert updated_task.status == "closed"

    # The rejection reaches the agent from the system, not from the task's own agent
    session_view = await services.sessions.get(project_id, "orchestrator")
    # The agent's own turn may add replies after it, so find the rejection by its text.
    rejections = [
        m
        for m in session_view.messages
        if any("Needs better formatting" in getattr(p, "text", "") for p in m.parts)
    ]
    assert len(rejections) == 1
    assert rejections[0].author_name == "system"
    assert rejections[0].from_agent == "system"


def _rejection_count(messages: list[MessageView]) -> int:
    return sum(
        1
        for m in messages
        if m.author_name == "system"
        and any("Local review rejected" in getattr(p, "text", "") for p in m.parts)
    )


class _FailingSessions:
    """Sessions whose post fails, like an agent session that cannot take a message."""

    async def post(self, *args: Any, **kwargs: Any) -> None:
        raise DomainError("session_unavailable", "The agent session is unavailable.", status=503)


async def test_decide_local_review_reject_with_failing_post_keeps_task_in_review(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Reject fails")
    await services.tasks.set_review(project_id, task.id, kind="local")

    with pytest.raises(DomainError) as exc_info:
        await decide_local_review(
            services.tasks,
            cast(AgentSessions, _FailingSessions()),
            project_id,
            task.id,
            approve=False,
        )
    assert exc_info.value.code == "session_unavailable"
    assert (await services.tasks.view(project_id, task.id)).status == "in_review"

    # The retry closes the task and tells the agent once.
    closed = await decide_local_review(
        services.tasks, services.sessions, project_id, task.id, approve=False
    )
    assert closed.status == "closed"
    session_view = await services.sessions.get(project_id, "orchestrator")
    assert _rejection_count(session_view.messages) == 1


async def test_decide_local_review_on_a_closed_task_answers_409(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Decide twice")
    await services.tasks.set_review(project_id, task.id, kind="local")
    await decide_local_review(services.tasks, services.sessions, project_id, task.id, approve=False)

    with pytest.raises(DomainError) as exc_info:
        await decide_local_review(
            services.tasks, services.sessions, project_id, task.id, approve=False
        )
    assert exc_info.value.status == 409
    session_view = await services.sessions.get(project_id, "orchestrator")
    assert _rejection_count(session_view.messages) == 1


async def test_decide_local_review_invalid_state(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")

    task = await services.tasks.start(project_id, session.id, "orchestrator", "Active Task")

    # Not in review
    with pytest.raises(DomainError) as exc_info:
        await decide_local_review(
            tasks=services.tasks,
            sessions=services.sessions,
            project_id=project_id,
            task_id=task.id,
            approve=True,
        )
    assert exc_info.value.status == 409


async def test_decide_local_review_concurrent_approvals(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task Concurrency")
    (task.worktree / "c.py").write_text("x = 1\n", encoding="utf-8")
    await services.tasks.commit(project_id, task, "feat: c")
    await services.tasks.set_review(project_id, task.id, kind="local")

    results = await asyncio.gather(
        decide_local_review(services.tasks, services.sessions, project_id, task.id, approve=True),
        decide_local_review(services.tasks, services.sessions, project_id, task.id, approve=True),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, DomainError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status == 409


async def test_get_github_activity(app: FastAPI, tmp_path: Path, git_env: dict[str, str]) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    events: EventLog = services.events
    session = await services.sessions.get(project_id, "orchestrator")

    # Record issue.opened
    await events.record(
        project_id,
        source="orchestrator",
        type="issue.opened",
        payload={
            "number": 10,
            "title": "Bug in pipeline",
            "url": "https://github.com/issues/10",
            "state": "open",
        },
        subject_id="10",
    )

    # Record issue.commented for same issue (newer)
    await events.record(
        project_id,
        source="orchestrator",
        type="issue.commented",
        payload={
            "number": 10,
            "url": "https://github.com/issues/10#issuecomment-1",
            "comment_id": 1,
        },
        subject_id="10",
    )

    # Open a github task with PR 100
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task PR")
    await services.tasks.set_review(
        project_id,
        task.id,
        kind="github",
        pr_number=100,
        pr_url="https://github.com/pull/100",
    )

    # Record pr.opened
    await events.record(
        project_id,
        source="orchestrator",
        type="pr.opened",
        payload={
            "task_id": task.id,
            "number": 100,
            "url": "https://github.com/pull/100",
            "title": "Add feature X",
        },
        subject_id=task.id,
    )

    # Record pr.opened for an older PR 99 whose task is not open
    await events.record(
        project_id,
        source="orchestrator",
        type="pr.opened",
        payload={
            "task_id": "tsk_ended",
            "number": 99,
            "url": "https://github.com/pull/99",
            "title": "Old PR",
        },
        subject_id="tsk_ended",
    )

    activity = await get_github_activity(events, services.tasks, project_id)

    # Should have 3 items: PR 100 (open), PR 99 (closed), Issue 10 (open)
    assert len(activity) == 3

    # PR 100 should have state "open" because its task is in open_tasks
    item_pr100 = next(i for i in activity if i.kind == "pr" and i.number == 100)
    assert item_pr100.state == "open"
    assert item_pr100.title == "Add feature X"

    # PR 99 should have state "closed" because it is not in open_tasks
    item_pr99 = next(i for i in activity if i.kind == "pr" and i.number == 99)
    assert item_pr99.state == "closed"

    # Issue 10 should preserve title from issue.opened
    item_issue10 = next(i for i in activity if i.kind == "issue" and i.number == 10)
    assert item_issue10.title == "Bug in pipeline"
    assert item_issue10.state == "open"

    # Ensure sorted by created_at descending
    created_ats = [i.created_at for i in activity]
    assert created_ats == sorted(created_ats, reverse=True)


async def test_github_activity_shows_the_real_state_of_an_issue(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services = app.state.services
    project_id = await _setup_project(app, tmp_path, git_env)
    events: EventLog = services.events

    await events.record(
        project_id,
        source="orchestrator",
        type="issue.opened",
        payload={"number": 20, "title": "Closed later", "url": "u20", "state": "open"},
        subject_id="20",
    )
    await events.record(
        project_id,
        source="orchestrator",
        type="issue.commented",
        payload={"number": 20, "title": "Closed later", "url": "u20", "state": "closed"},
        subject_id="20",
    )
    # Opened already closed; a later comment from an older event shape has no state.
    await events.record(
        project_id,
        source="orchestrator",
        type="issue.opened",
        payload={"number": 21, "title": "Born closed", "url": "u21", "state": "closed"},
        subject_id="21",
    )
    await events.record(
        project_id,
        source="orchestrator",
        type="issue.commented",
        payload={"number": 21, "url": "u21"},
        subject_id="21",
    )

    activity = await get_github_activity(events, services.tasks, project_id)
    states = {i.number: i.state for i in activity if i.kind == "issue"}
    assert states == {20: "closed", 21: "closed"}
