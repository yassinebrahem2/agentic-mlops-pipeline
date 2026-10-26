"""PR state background sync observer (spec §6)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from mlops_agents.integrations.github.pulls import get_pull

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.runtime.contract import TurnEnd
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.integrations.github.auth import GitHubAuth

logger = logging.getLogger(__name__)


class PrSyncObserver:
    """Synchronizes open task states with their GitHub pull requests at turn start."""

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
    ) -> None:
        self._tasks = tasks
        self._github = github
        self._http = http
        self._last_sync: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

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

        try:
            open_tasks = await self._tasks.open_tasks(project_id)
        except Exception as exc:
            logger.warning("PR sync could not load open tasks for project %s: %s", project_id, exc)
            return

        repo_slug = f"{ref.owner}/{ref.repo}"
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
                try:
                    await self._tasks.end(project_id, task.id, status="merged")
                except Exception as exc:
                    logger.warning("PR sync could not end task %s as merged: %s", task.id, exc)
            elif pr.state == "closed":
                try:
                    await self._tasks.end(project_id, task.id, status="closed")
                except Exception as exc:
                    logger.warning("PR sync could not end task %s as closed: %s", task.id, exc)

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
