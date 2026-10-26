"""Local reviews management and GitHub activity aggregation (I13)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from mlops_agents.agents.constants import cap_diff
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.agents.runtime.contract import AgentSessions
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.service import WorkspaceService


class LocalReview(BaseModel):
    task: TaskView
    agent: str
    files_changed: list[str]
    diff: str


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str | None = None


class GitHubItem(BaseModel):
    kind: Literal["pr", "issue"]
    number: int
    title: str
    url: str
    state: str
    agent: str
    created_at: str


async def list_local_reviews(
    tasks: Tasks,
    workspace: WorkspaceService,
    git: GitRunner,
    empty_hooks: Path,
    project_id: str,
) -> list[LocalReview]:
    """Lists tasks in project in local review with their changed files and capped diff."""
    open_tasks = await tasks.open_tasks(project_id)
    local_tasks = [t for t in open_tasks if t.status == "in_review" and t.review_kind == "local"]
    if not local_tasks:
        return []

    project = await workspace.get(project_id)
    repo_root = workspace.repo_root(project)
    default_branch = project.default_branch or "main"

    reviews: list[LocalReview] = []
    for t in local_tasks:
        task_ref = TaskRef(
            id=t.id,
            title=t.title,
            branch=t.branch,
            worktree=Path(),
            status=t.status,
        )
        files_changed = await tasks.local_merge_preview(project_id, task_ref)
        res = await run_safe_git(
            git,
            args=["diff", f"{default_branch}...{t.branch}"],
            cwd=repo_root,
            empty_hooks=empty_hooks,
        )
        diff = cap_diff(res.stdout)
        reviews.append(
            LocalReview(
                task=t,
                agent=t.agent,
                files_changed=files_changed,
                diff=diff,
            )
        )
    return reviews


async def decide_local_review(
    tasks: Tasks,
    sessions: AgentSessions,
    project_id: str,
    task_id: str,
    *,
    approve: bool,
    note: str | None = None,
) -> TaskView:
    """Approves (merges) or rejects (closes) a task in local review."""
    task = await tasks.view(project_id, task_id)
    if task.status != "in_review" or task.review_kind != "local":
        raise DomainError(
            "not_in_review",
            f"Task '{task_id}' is not awaiting local review.",
            status=400,
        )

    task_ref = TaskRef(
        id=task.id,
        title=task.title,
        branch=task.branch,
        worktree=Path(),
        status=task.status,
    )

    if approve:
        try:
            await tasks.local_merge(project_id, task_ref)
        except CommitRefused as err:
            raise DomainError("conflict", str(err), status=409) from err
        await tasks.end(project_id, task_id, status="merged")
    else:
        await tasks.end(project_id, task_id, status="closed")
        rejection_text = (
            f"Local review rejected for task '{task.title}': {note}"
            if note
            else f"Local review rejected for task '{task.title}'."
        )
        await sessions.post(
            project_id,
            task.agent,
            author_name="Review",
            from_agent=task.agent,
            text=rejection_text,
            exchange_id=None,
            hop=None,
        )

    return await tasks.view(project_id, task_id)


async def get_github_activity(
    events: EventLog,
    tasks: Tasks,
    project_id: str,
) -> list[GitHubItem]:
    """Aggregates PR and issue activity from the event log and open tasks."""
    records = await events.history(
        project_id,
        types=["pr.opened", "pr.updated", "issue.opened", "issue.commented"],
        limit=200,
    )
    open_tasks = await tasks.open_tasks(project_id)
    open_pr_numbers = {
        t.pr_number for t in open_tasks if t.pr_number is not None and t.review_kind == "github"
    }

    items_by_key: dict[tuple[Literal["pr", "issue"], int], GitHubItem] = {}

    for record in records:
        event_type = record.type
        kind: Literal["pr", "issue"]
        if event_type in ("pr.opened", "pr.updated"):
            kind = "pr"
        elif event_type in ("issue.opened", "issue.commented"):
            kind = "issue"
        else:
            continue

        raw_num = record.payload.get("number")
        if raw_num is None:
            continue
        try:
            num = int(raw_num)
        except (ValueError, TypeError):
            continue

        url = str(record.payload.get("url") or "")
        title = str(record.payload.get("title") or "")
        agent = record.source
        created_at = record.ts

        if kind == "pr":
            state = (
                "open" if num in open_pr_numbers else str(record.payload.get("state") or "closed")
            )
            if not title:
                title = f"PR #{num}"
        else:
            state = str(record.payload.get("state") or "open")
            if not title:
                title = f"Issue #{num}"

        key = (kind, num)
        if key not in items_by_key:
            items_by_key[key] = GitHubItem(
                kind=kind,
                number=num,
                title=title,
                url=url,
                state=state,
                agent=agent,
                created_at=created_at,
            )
        else:
            existing = items_by_key[key]
            updated_title = existing.title
            if (existing.title == f"Issue #{num}" and title != f"Issue #{num}") or (
                existing.title == f"PR #{num}" and title != f"PR #{num}"
            ):
                updated_title = title

            updated_url = existing.url or url
            if updated_title != existing.title or updated_url != existing.url:
                items_by_key[key] = existing.model_copy(
                    update={"title": updated_title, "url": updated_url}
                )

    result = list(items_by_key.values())
    result.sort(key=lambda item: item.created_at, reverse=True)
    return result
