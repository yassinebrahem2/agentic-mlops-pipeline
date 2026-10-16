import os
from collections.abc import Iterator

import pytest


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Identity and isolation for git commands run by tests."""
    values = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    yield {**os.environ, **values}
