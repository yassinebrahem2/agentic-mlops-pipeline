from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import httpx2
from pydantic import BaseModel, Field, ValidationError, field_validator

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import (
    PERMISSION_ISSUES_WRITE,
    request,
    unreadable,
)


class IssueSummary(BaseModel):
    number: int
    title: str
    body: str | None = None
    html_url: str
    state: str = "open"
    labels: list[str] = Field(default_factory=list)

    @field_validator("labels", mode="before")
    @classmethod
    def _normalize_labels(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        result: list[str] = []
        raw_list: list[object] = cast(list[object], v)
        for item in raw_list:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                raw_dict: dict[object, object] = cast(dict[object, object], item)
                val = raw_dict.get("name")
                if isinstance(val, str):
                    result.append(val)
        return result


class IssueComment(BaseModel):
    id: int
    body: str
    html_url: str


async def search_issues(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    query: str = "",
) -> list[IssueSummary]:
    q_parts = [f"repo:{repo}", "is:issue", "is:open"]
    if query.strip():
        q_parts.append(query.strip())
    q = " ".join(q_parts)

    response = await request(
        http,
        "GET",
        "/search/issues",
        token,
        params={"q": q},
        permission=PERMISSION_ISSUES_WRITE,
    )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        data: object = response.json()
        if isinstance(data, list):
            items: list[object] = cast(list[object], data)
        elif isinstance(data, dict):
            raw_dict: dict[object, object] = cast(dict[object, object], data)
            raw_items = raw_dict.get("items")
            items = cast(list[object], raw_items) if isinstance(raw_items, list) else []
        else:
            items = []
        return [IssueSummary.model_validate(item) for item in items]
    except (ValidationError, ValueError):
        raise unreadable() from None


async def create_issue(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> IssueSummary:
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels is not None:
        payload["labels"] = list(labels)

    response = await request(
        http,
        "POST",
        f"/repos/{repo}/issues",
        token,
        json=payload,
        permission=PERMISSION_ISSUES_WRITE,
    )
    if response.status_code not in (200, 201):
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return IssueSummary.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None


async def comment_issue(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
    body: str,
) -> IssueComment:
    payload = {"body": body}
    response = await request(
        http,
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        token,
        json=payload,
        permission=PERMISSION_ISSUES_WRITE,
    )
    if response.status_code not in (200, 201):
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return IssueComment.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None


async def ensure_labels(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    labels: Sequence[str],
) -> None:
    for label in labels:
        response = await request(
            http,
            "POST",
            f"/repos/{repo}/labels",
            token,
            json={"name": label},
            permission=PERMISSION_ISSUES_WRITE,
        )
        if response.status_code in (200, 201, 422):
            continue
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
