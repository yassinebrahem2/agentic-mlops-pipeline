import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from mlops_agents.config import RunMode, load_settings
from mlops_agents.credentials import KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


class BreakableKeyStore(MemoryKeyStore):
    """A key store whose reads can be made to fail, like a locked or missing OS keyring."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def get(self, ref: str) -> str | None:
        if self.broken:
            raise KeyStoreError("The OS keyring is locked. Unlock it and try again.")
        return super().get(ref)


@dataclass
class Harness:
    svc: WorkspaceService
    github: GitHubAuth
    gh: FakeGh
    keystore: BreakableKeyStore
    revoked: set[str] = field(default_factory=set[str])


def _bare_remote(tmp_path: Path, env: dict[str, str]) -> tuple[Path, Path]:
    work = make_repo(tmp_path / "work", {"README.md": "first"}, env)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)], env=env, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
    )
    return work, bare


def _force_push_rewrite(work: Path, env: dict[str, str]) -> None:
    (work / "README.md").write_text("rewritten")
    for args in (["commit", "-am", "rewrite", "--amend"], ["push", "--force", "origin", "main"]):
        subprocess.run(["git", *args], cwd=work, env=env, check=True, capture_output=True)


async def _harness(paths: DataPaths, databases: Databases, bare: Path) -> Harness:
    revoked: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").removeprefix("Bearer ") in revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if request.url.path.lower() == "/repos/octo/demo":
            repo = {"full_name": "Octo/Demo", "private": True, "default_branch": "main"}
            return httpx.Response(200, json={**repo, "description": None})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    keystore = BreakableKeyStore()
    gh = FakeGh("gho_test")
    github = GitHubAuth(databases, keystore, http, gh)
    await github.enable_gh()
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    service = WorkspaceService(
        settings,
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        remote_url=lambda _: bare.as_uri(),
    )
    return Harness(service, github, gh, keystore, revoked)


async def test_add_github_clones_into_app_data(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    svc = (await _harness(paths, databases, bare)).svc
    project = await svc.add_github("https://github.com/octo/demo")
    assert (project.name, project.github_full_name, project.status) == ("Demo", "Octo/Demo", "ok")
    assert (svc.repo_root(project) / "README.md").read_text() == "first"
    with pytest.raises(DomainError) as err:
        await svc.add_github("OCTO/DEMO")
    assert err.value.code == "project_exists"


async def test_refresh_survives_force_push(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    work, bare = _bare_remote(tmp_path, git_env)
    svc = (await _harness(paths, databases, bare)).svc
    project = await svc.add_github("octo/demo")
    await asyncio.to_thread(_force_push_rewrite, work, git_env)
    refreshed = await svc.refresh(project.id)
    assert refreshed.status == "ok"
    assert (svc.repo_root(project) / "README.md").read_text() == "rewritten"


async def test_failed_refresh_keeps_the_last_copy(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    svc = (await _harness(paths, databases, bare)).svc
    project = await svc.add_github("octo/demo")
    bare.rename(tmp_path / "gone.git")  # the remote is gone, like a deleted repo
    refreshed = await svc.refresh(project.id)
    assert refreshed.status == "ok"
    assert refreshed.status_detail is not None
    assert refreshed.status_detail.startswith("Refresh failed")
    assert (svc.repo_root(project) / "README.md").read_text() == "first"


async def test_revoked_pasted_token_says_what_to_do(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    h = await _harness(paths, databases, bare)
    await h.github.set_token("ghp_pasted")
    project = await h.svc.add_github("octo/demo")
    h.revoked.add("ghp_pasted")  # revoked or expired on github.com
    bare.rename(tmp_path / "gone.git")  # so git's fetch fails as it would with the revoked token
    opened = await h.svc.open(project.id)
    assert opened.status == "ok"
    assert opened.status_detail is not None
    assert opened.status_detail.startswith("Refresh failed: GitHub rejected the token.")
    assert "paste a new token" in opened.status_detail
    assert (h.svc.repo_root(project) / "README.md").read_text() == "first"


async def test_gh_logout_after_add_keeps_the_last_copy(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    h = await _harness(paths, databases, bare)
    project = await h.svc.add_github("octo/demo")
    h.gh.value = None  # the user logged out of gh
    opened = await h.svc.open(project.id)
    assert opened.status == "ok"
    assert opened.status_detail is not None
    assert opened.status_detail.startswith("Refresh failed: not logged in.")
    assert (h.svc.repo_root(project) / "README.md").read_text() == "first"


async def test_unreadable_key_store_keeps_the_last_copy(
    tmp_path: Path, paths: DataPaths, databases: Databases, git_env: dict[str, str]
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    h = await _harness(paths, databases, bare)
    await h.github.set_token("ghp_pasted")
    project = await h.svc.add_github("octo/demo")
    h.keystore.broken = True
    opened = await h.svc.open(project.id)
    assert opened.status == "ok"
    assert opened.status_detail is not None
    assert opened.status_detail.startswith("Refresh failed: The saved GitHub token could not")
    assert (h.svc.repo_root(project) / "README.md").read_text() == "first"
