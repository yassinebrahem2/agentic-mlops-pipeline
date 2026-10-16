import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases


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


def make_repo(path: Path, files: dict[str, str], env: dict[str, str]) -> Path:
    """A git repository at `path` with `files` committed on branch main."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, env=env, check=True, capture_output=True
    )
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if files:
        subprocess.run(["git", "add", "-A"], cwd=path, env=env, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=path, env=env, check=True, capture_output=True
        )
    return path


class MemoryKeyStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.data.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.data[ref] = value

    def delete(self, ref: str) -> None:
        self.data.pop(ref, None)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


@pytest.fixture
async def databases(paths: DataPaths) -> AsyncIterator[Databases]:
    dbs = Databases(paths)
    yield dbs
    await dbs.dispose()


@pytest.fixture
def keystore() -> MemoryKeyStore:
    return MemoryKeyStore()
