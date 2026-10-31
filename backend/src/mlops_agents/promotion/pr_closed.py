"""Closed unmerged pull requests observer (I38, roadmap)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.merge import get_last_issue_comment
from mlops_agents.integrations.github.pulls import get_pull
from mlops_agents.knowledge.contract import NoteDraft
from mlops_agents.monitoring.untrusted import untrusted

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.pr_sync import PrChange
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.knowledge.contract import Knowledge

logger = logging.getLogger(__name__)


class ClosedUnmergedObserver:
    """Creates a decision Knowledge note when a PR is closed without being merged."""

    def __init__(
        self,
        knowledge: Knowledge,
        http: httpx2.AsyncClient,
        github: GitHubAuth,
        tasks: Tasks,
    ) -> None:
        self._knowledge = knowledge
        self._http = http
        self._github = github
        self._tasks = tasks
        self._seen: set[tuple[str, int]] = set()

    async def changed(self, project_id: str, pr: PrChange) -> None:
        if pr.state != "closed":
            return
        if (project_id, pr.number) in self._seen:
            return

        note_id = f"kno_closed_pr_{pr.number}"
        try:
            existing = await self._knowledge.get(project_id, note_id)
            if existing is not None:
                self._seen.add((project_id, pr.number))
                return
        except Exception as exc:
            logger.debug("Could not check existing note for PR #%s: %s", pr.number, exc)

        token = await self._github.token()
        if not token:
            return
        ref = await self._tasks.repo_ref(project_id)
        if ref is None:
            return
        repo_slug = f"{ref.owner}/{ref.repo}"

        try:
            pr_obj = await get_pull(self._http, token, repo_slug, pr.number)
            title = pr_obj.title
            pr_body = pr_obj.body or ""
        except Exception as exc:
            logger.warning("Could not fetch PR #%s for note creation: %s", pr.number, exc)
            title = f"PR #{pr.number}"
            pr_body = ""

        try:
            last_comment = await get_last_issue_comment(self._http, token, repo_slug, pr.number)
        except Exception as exc:
            logger.warning("Could not fetch last comment for PR #%s: %s", pr.number, exc)
            last_comment = None

        comment_text = last_comment if last_comment is not None else pr_body
        text = untrusted(comment_text, "github_comment")
        draft = NoteDraft(
            type="decision",
            title=f"PR #{pr.number} closed unmerged: {title}",
            text=text,
        )

        try:
            await self._knowledge.save(
                project_id,
                author="system",
                note=draft,
                note_id=note_id,
            )
        except DomainError as err:
            if err.code == "note_not_found":
                await self._knowledge.save(
                    project_id,
                    author="system",
                    note=draft,
                )
            else:
                raise
        self._seen.add((project_id, pr.number))
