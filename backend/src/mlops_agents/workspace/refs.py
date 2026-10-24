import os
import re
from dataclasses import dataclass
from pathlib import Path

from mlops_agents.errors import DomainError
from mlops_agents.workspace.models import Project

_OWNER = r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
_REPO = r"(?P<repo>[A-Za-z0-9._-]+?)"
_PATTERNS = (
    re.compile(rf"^https?://github\.com/{_OWNER}/{_REPO}(?:\.git)?/?$"),
    re.compile(rf"^git@github\.com:{_OWNER}/{_REPO}(?:\.git)?$"),
    re.compile(rf"^{_OWNER}/(?P<repo>[A-Za-z0-9._-]+)$"),
)


def path_key(path: Path) -> str:
    """A comparable identity for a folder: resolved, and case-folded on Windows."""
    resolved = str(path.resolve(strict=True))
    return resolved.casefold() if os.name == "nt" else resolved


def parse_github_ref(text: str) -> str:
    value = text.strip()
    for pattern in _PATTERNS:
        match = pattern.match(value)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    raise DomainError(
        "invalid_github_repo", "Enter a GitHub repository as owner/name or its URL.", status=422
    )


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str


def repo_ref(full_name: str) -> RepoRef:
    owner, _, repo = full_name.partition("/")
    return RepoRef(owner=owner, repo=repo)


def https_url(ref: RepoRef) -> str:
    return f"https://github.com/{ref.owner}/{ref.repo}.git"


def github_remote(project: Project, origin_url: str | None) -> RepoRef | None:
    """The GitHub repository a project's work is pushed to, or None when it has none.

    A GitHub project is known by its full name. A local project is known by its `origin` URL, which
    only counts when it is a github.com URL: the owner/name shorthand is not a remote, and would
    match a relative path such as `foo/bar`.
    """
    if project.github_full_name:
        return repo_ref(project.github_full_name)
    if not origin_url:
        return None
    for pattern in _PATTERNS[:2]:
        match = pattern.match(origin_url.strip())
        if match:
            return RepoRef(owner=match.group("owner"), repo=match.group("repo"))
    return None
