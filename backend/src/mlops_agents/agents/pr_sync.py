"""PR state background sync observer (spec §6)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.issues import get_issue
from mlops_agents.integrations.github.merge import (
    PASSING_CONCLUSIONS,
    get_check_runs,
    get_pull_merge_state,
)
from mlops_agents.integrations.github.pulls import get_pull, list_pull_files

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.runtime.contract import AgentSessions, TaskView, TurnEnd
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


class PrChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    state: Literal["open", "merged", "closed"]
    merged_at: datetime | None
    head_branch: str
    agent: str
    files: list[str]


class PrObserver(Protocol):
    async def changed(self, project_id: str, pr: PrChange) -> None: ...


class PrSyncObserver:
    """Synchronizes open task states with their GitHub pull requests at turn start."""

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
        events: EventLog,
        sessions: AgentSessions | None = None,
        workspace: WorkspaceService | None = None,
    ) -> None:
        self._events = events
        self._tasks = tasks
        self._github = github
        self._http = http
        self._sessions = sessions
        self._workspace = workspace
        self._last_sync: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._observers: list[PrObserver] = []

    def bind_sessions(self, sessions: AgentSessions) -> None:
        self._sessions = sessions

    def bind_workspace(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def add_observer(self, observer: PrObserver) -> None:
        self._observers.append(observer)

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        now = time.monotonic()
        last = self._last_sync.get(project_id)
        if last is not None and (now - last) < 60.0:
            return
        self._last_sync[project_id] = now
        task = asyncio.create_task(self._sync_safely(project_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        pass

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        pass

    async def _sync_safely(self, project_id: str) -> None:
        try:
            await self._do_sync(project_id)
        except Exception as exc:
            logger.warning("PR sync error for project %s: %s", project_id, exc)

    async def _do_sync(self, project_id: str) -> None:
        try:
            token = await self._github.token()
        except Exception as exc:
            logger.warning(
                "PR sync could not retrieve GitHub token for project %s: %s",
                project_id,
                exc,
            )
            return
        if not token:
            return

        ref = await self._tasks.repo_ref(project_id)
        if ref is None:
            return

        repo_slug = f"{ref.owner}/{ref.repo}"
        try:
            open_tasks = await self._tasks.open_tasks(project_id)
        except Exception as exc:
            logger.warning("PR sync could not load open tasks for project %s: %s", project_id, exc)
            return

        for task in open_tasks:
            if task.pr_number is None:
                continue
            if task.review_kind is not None and task.review_kind != "github":
                continue

            try:
                pr = await get_pull(self._http, token, repo_slug, task.pr_number)
            except Exception as exc:
                logger.warning(
                    "PR sync could not check PR #%s for task %s in %s: %s",
                    task.pr_number,
                    task.id,
                    repo_slug,
                    exc,
                )
                continue

            if pr.merged:
                state = "merged"
                merged_at = pr.merged_at
            elif pr.state == "closed":
                state = "closed"
                merged_at = pr.merged_at
            else:
                await self._check_ci_and_wake(project_id, task, token, repo_slug)
                continue

            try:
                pull_files = await list_pull_files(self._http, token, repo_slug, task.pr_number)
            except Exception as exc:
                logger.warning(
                    "PR sync could not list files for PR #%s in %s: %s",
                    task.pr_number,
                    repo_slug,
                    exc,
                )
                continue

            files: list[str] = []
            for pf in pull_files:
                if pf.previous_filename and pf.previous_filename not in files:
                    files.append(pf.previous_filename)
                if pf.filename not in files:
                    files.append(pf.filename)

            try:
                await self._tasks.end(project_id, task.id, status=state)
            except Exception as exc:
                logger.warning("PR sync could not end task %s as %s: %s", task.id, state, exc)
                continue

            change = PrChange(
                number=task.pr_number,
                state=state,
                merged_at=merged_at,
                head_branch=task.branch,
                agent=task.agent,
                files=files,
            )
            for observer in self._observers:
                try:
                    await observer.changed(project_id, change)
                except Exception as exc:
                    logger.warning(
                        "PrObserver %s failed on project %s: %s", observer, project_id, exc
                    )

        # After the PRs, so a failing issue refresh never keeps a merged PR from ending its task.
        try:
            await self._refresh_issues(project_id, token, repo_slug)
        except Exception as exc:
            logger.warning("PR sync could not refresh issues for project %s: %s", project_id, exc)

    async def _refresh_issues(self, project_id: str, token: str, repo_slug: str) -> None:
        """Records issue.updated for every issue the agents touched whose state changed on GitHub,
        so the activity feed shows an issue closed by hand or by a merged PR as closed."""
        recorded = await self._events.history(
            project_id, types=["issue.opened", "issue.commented", "issue.updated"], limit=200
        )
        last_state: dict[int, str] = {}
        for record in reversed(recorded):  # oldest first, so the newest record wins
            number = record.payload.get("number")
            if isinstance(number, int):
                last_state[number] = str(record.payload.get("state") or "open")

        for number, state in last_state.items():
            try:
                issue = await get_issue(self._http, token, repo_slug, number)
            except DomainError as exc:
                if exc.code == "issue_gone":
                    # Its last recorded state stands; nothing is left to refresh.
                    logger.debug("PR sync skipped deleted issue #%s in %s", number, repo_slug)
                    continue
                logger.warning(
                    "PR sync could not check issue #%s in %s: %s", number, repo_slug, exc
                )
                continue
            if issue.state != state:
                await self._events.record(
                    project_id,
                    source="system",
                    type="issue.updated",
                    payload=issue.event_payload(),
                    subject_id=str(number),
                )

    async def _check_ci_and_wake(
        self,
        project_id: str,
        task: TaskView,
        token: str,
        repo_slug: str,
    ) -> None:
        if task.pr_number is None:
            return
        if task.ci_wakes >= 3:
            return
        try:
            merge_state = await get_pull_merge_state(self._http, token, repo_slug, task.pr_number)
        except Exception as exc:
            logger.warning(
                "PR sync could not check merge state for PR #%s in %s: %s",
                task.pr_number,
                repo_slug,
                exc,
            )
            return

        head_sha = merge_state.head_sha
        if not head_sha or task.ci_woken_sha == head_sha:
            return

        try:
            head_runs = await get_check_runs(self._http, token, repo_slug, head_sha)
        except Exception as exc:
            logger.warning(
                "PR sync could not get check runs for %s in %s: %s",
                head_sha,
                repo_slug,
                exc,
            )
            return

        if not head_runs:
            return

        if any(r.status != "completed" for r in head_runs):
            return

        head_failing = [r for r in head_runs if r.conclusion not in PASSING_CONCLUSIONS]
        if not head_failing:
            return

        default_branch = "main"
        if self._workspace is not None:
            try:
                proj = await self._workspace.get(project_id)
                default_branch = proj.default_branch or "main"
            except Exception as exc:
                logger.debug(
                    "PR sync could not determine default branch for project %s: %s",
                    project_id,
                    exc,
                )

        try:
            main_runs = await get_check_runs(self._http, token, repo_slug, default_branch)
        except Exception as exc:
            logger.warning(
                "PR sync could not get check runs for default branch %s in %s: %s",
                default_branch,
                repo_slug,
                exc,
            )
            main_runs = []

        main_failing_names = {
            r.name
            for r in main_runs
            if r.status == "completed" and r.conclusion not in PASSING_CONCLUSIONS
        }
        failing_to_wake = [r for r in head_failing if r.name not in main_failing_names]
        if not failing_to_wake:
            return

        check_lines: list[str] = []
        for r in failing_to_wake:
            link = r.details_url or r.html_url or ""
            link_str = f" ({link})" if link else ""
            check_lines.append(f"- {r.name}: {r.conclusion}{link_str}")

        wake_count = task.ci_wakes + 1
        msg_parts = [
            f"CI failed on PR #{task.pr_number} for task {task.id}:",
            *check_lines,
            "",
            f"Resume the task with ResumeTask {task.id}, reproduce by running the commands of the team's CI workflow in the sandbox, fix it and push with OpenPullRequest (it updates the same PR).",
        ]
        if wake_count == 3:
            msg_parts.extend(
                [
                    "",
                    "This is the last CI wake for this task: if it stays red, say so in your session and stop.",
                ]
            )
        text = "\n".join(msg_parts)

        if self._sessions is not None:
            try:
                await self._sessions.post(
                    project_id,
                    task.agent,
                    author_name="system",
                    from_agent="system",
                    text=text,
                    exchange_id=None,
                    hop=None,
                    system=True,
                )
            except Exception as exc:
                logger.warning(
                    "PR sync could not post CI wake message for task %s to session: %s",
                    task.id,
                    exc,
                )
                return

        try:
            await self._tasks.record_ci_wake(project_id, task.id, head_sha)
        except Exception as exc:
            logger.warning(
                "PR sync could not record CI wake for task %s (sha %s): %s",
                task.id,
                head_sha,
                exc,
            )

    async def sync_now(self, project_id: str) -> None:
        """Manually run sync for a project, awaiting its completion."""
        await self._sync_safely(project_id)

    async def wait_active(self) -> None:
        """Wait for any active background sync tasks to finish."""
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    async def close(self) -> None:
        """Cancel and wait for any active background tasks."""
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
