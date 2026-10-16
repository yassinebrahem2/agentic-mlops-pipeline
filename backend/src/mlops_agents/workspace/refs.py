import os
import re
from pathlib import Path

from mlops_agents.errors import DomainError

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
