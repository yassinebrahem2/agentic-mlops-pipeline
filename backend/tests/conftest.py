import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from mlops_agents.paths import DataPaths
from mlops_agents.sandbox.docker import CliDockerRunner
from mlops_agents.storage.databases import Databases
from tests.fakes.fake_openai import FakeState, RunningFake, start_fake_server

STAGE_2_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "Bash",
        "StartTask",
        "ResumeTask",
        "OpenPullRequest",
        "SearchIssues",
        "OpenIssue",
        "CommentOnIssue",
        "ListAgents",
        "SendMessage",
        "ScheduleWakeup",
    }
)


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Identity and isolation for git commands run by tests."""
    values = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
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


@pytest.fixture
async def model_http() -> AsyncIterator[httpx2.AsyncClient]:
    """The shared client the app passes to model calls, closed after the test."""
    async with httpx2.AsyncClient() as client:
        yield client


@pytest.fixture(scope="session")
def _fake_openai_server() -> Iterator[RunningFake]:
    running = start_fake_server(FakeState())
    yield running
    running.stop()


@pytest.fixture
def fake_openai(_fake_openai_server: RunningFake) -> Iterator[RunningFake]:
    _fake_openai_server.state.reset()
    try:
        yield _fake_openai_server
    finally:
        _fake_openai_server.state.release_slow()


@pytest.fixture(autouse=True)
def guard_docker_cli(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    item = cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]  # pytest FixtureRequest.node lacks type stubs
    if item.get_closest_marker("docker") is not None:
        return

    original_run = CliDockerRunner.run

    async def _guarded_run(self: CliDockerRunner, *args: Any, **kwargs: Any) -> Any:
        if self.executable == "docker":
            node_id = str(item.nodeid)
            pytest.fail(
                f"Test '{node_id}' attempted to reach the real Docker CLI "
                "without the @pytest.mark.docker marker. Provide a fake Docker runner instead."
            )
        return await original_run(self, *args, **kwargs)

    monkeypatch.setattr(CliDockerRunner, "run", _guarded_run)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("timeout") is not None:
            continue
        if item.get_closest_marker("docker") is not None:
            item.add_marker(pytest.mark.timeout(1200))
        elif item.get_closest_marker("live") is not None:
            item.add_marker(pytest.mark.timeout(600))
