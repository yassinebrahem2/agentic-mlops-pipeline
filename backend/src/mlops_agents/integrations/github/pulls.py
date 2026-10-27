from __future__ import annotations

from typing import Any

import httpx2
from pydantic import BaseModel, ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import (
    PERMISSION_PULL_REQUESTS_WRITE,
    permission_missing,
    request,
    unreadable,
)


class PullRequest(BaseModel):
    number: int
    title: str
    body: str | None = None
    html_url: str
    state: str = "open"
    merged: bool = False


async def create_pull(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    *,
    title: str,
    head: str,
    base: str,
    body: str | None = None,
) -> PullRequest:
    payload: dict[str, Any] = {"title": title, "head": head, "base": base}
    if body is not None:
        payload["body"] = body

    response = await request(
        http,
        "POST",
        f"/repos/{repo}/pulls",
        token,
        json=payload,
        permission=PERMISSION_PULL_REQUESTS_WRITE,
    )
    if response.status_code not in (200, 201):
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return PullRequest.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None


async def update_pull(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
) -> PullRequest:
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state

    response = await request(
        http,
        "PATCH",
        f"/repos/{repo}/pulls/{number}",
        token,
        json=payload,
        permission=PERMISSION_PULL_REQUESTS_WRITE,
    )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return PullRequest.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None


async def get_pull(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
) -> PullRequest:
    response = await request(
        http,
        "GET",
        f"/repos/{repo}/pulls/{number}",
        token,
    )
    # A 404 here means the PR was deleted (the sync reads PRs it opened itself), so it is
    # not reported as a missing permission like the write calls do.
    if response.status_code == 404:
        raise DomainError(
            "github_pr_not_found", f"Pull request #{number} no longer exists.", status=404
        )
    if response.status_code == 403:
        raise permission_missing(PERMISSION_PULL_REQUESTS_WRITE)
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return PullRequest.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None
