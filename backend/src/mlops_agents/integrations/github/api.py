from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError

GITHUB_API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class RepoSummary(BaseModel):
    full_name: str
    private: bool
    default_branch: str
    description: str | None


async def _get(
    http: httpx2.AsyncClient, token: str, path: str, params: dict[str, Any] | None = None
) -> httpx2.Response:
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        response = await http.get(GITHUB_API + path, headers=headers, params=params, timeout=30)
    except httpx2.HTTPError as exc:
        raise DomainError(
            "github_unreachable", f"Could not reach GitHub: {error_text(exc)}", status=502
        ) from None
    if response.status_code == 401:
        raise DomainError(
            "github_token_rejected",
            "GitHub rejected the token. Log in again with `gh auth login` or paste a new token in Integrations.",
            status=422,
        )
    if response.status_code == 429 or (
        response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
    ):
        raise DomainError(
            "github_rate_limited",
            f"GitHub's rate limit was reached. Try again {_retry_time(response.headers)}.",
            status=429,
        )
    return response


def _retry_time(headers: httpx2.Headers) -> str:
    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        return f"after {retry_after} seconds"
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return f"after {datetime.fromtimestamp(int(reset), UTC):%H:%M} UTC"
    return "in a few minutes"


class _User(BaseModel):
    login: str


_REPO_LIST = TypeAdapter(list[RepoSummary])


def _unreadable() -> DomainError:
    return DomainError(
        "github_error", "GitHub answered with data this app cannot read. Try again.", status=502
    )


async def get_login(http: httpx2.AsyncClient, token: str) -> str:
    response = await _get(http, token, "/user")
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return _User.model_validate(response.json()).login
    except ValidationError:
        raise _unreadable() from None


async def list_repos(
    http: httpx2.AsyncClient, token: str, *, limit: int = 300
) -> list[RepoSummary]:
    repos: list[RepoSummary] = []
    page = 1
    while len(repos) < limit:
        response = await _get(
            http, token, "/user/repos", {"per_page": 100, "page": page, "sort": "updated"}
        )
        if response.status_code != 200:
            raise DomainError(
                "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
            )
        try:
            page_repos = _REPO_LIST.validate_python(response.json())
        except ValidationError:
            raise _unreadable() from None
        repos.extend(page_repos)
        if len(page_repos) < 100:
            break
        page += 1
    return repos[:limit]


async def get_repo(http: httpx2.AsyncClient, token: str, full_name: str) -> RepoSummary:
    response = await _get(http, token, f"/repos/{full_name}")
    if response.status_code == 404:
        raise DomainError(
            "github_repo_not_found",
            f"{full_name} does not exist, or this GitHub account cannot see it.",
            status=404,
        )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return RepoSummary.model_validate(response.json())
    except ValidationError:
        raise _unreadable() from None
