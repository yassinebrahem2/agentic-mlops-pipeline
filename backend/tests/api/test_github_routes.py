from __future__ import annotations

import subprocess
from pathlib import Path

import httpx2
from fastapi import FastAPI

from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.workspace.service import WorkspaceService, github_https_url
from tests.api.conftest import NoGh
from tests.conftest import make_repo


class MockGitHubContext:
    def __init__(self, bare: Path | None = None) -> None:
        self.revoked: set[str] = set()
        self.bare = bare

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token in self.revoked or token == "ghp_bad":
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            if token in ("ghp_valid", "good_token"):
                return httpx2.Response(200, json={"login": "octo"})
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user/repos":
            if token in ("ghp_valid", "good_token"):
                return httpx2.Response(
                    200,
                    json=[
                        {
                            "full_name": "octo/demo",
                            "private": True,
                            "default_branch": "main",
                            "description": "Octocat Demo",
                        }
                    ],
                )
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if request.url.path.lower() == "/repos/octo/demo":
            if token in ("ghp_valid", "good_token"):
                repo = {"full_name": "Octo/Demo", "private": True, "default_branch": "main"}
                return httpx2.Response(200, json={**repo, "description": "Octocat Demo"})
            return httpx2.Response(401, json={"message": "Bad credentials"})
        return httpx2.Response(404, json={"message": "Not Found"})


def _setup_mock_github(app: FastAPI, ctx: MockGitHubContext) -> None:
    services = app.state.services
    mock_http = httpx2.AsyncClient(transport=httpx2.MockTransport(ctx.handler))
    github = GitHubAuth(services.databases, services.keystore, mock_http, NoGh())
    git = GitRunner(services.paths.bin, token_provider=github.token)
    bare = ctx.bare

    def remote_url(full_name: str) -> str:
        if bare is not None:
            return bare.as_uri()
        return github_https_url(full_name)

    workspace = WorkspaceService(
        services.settings,
        services.paths,
        services.databases,
        git,
        github,
        mock_http,
        remote_url=remote_url,
    )
    services.github = github
    services.workspace = workspace


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


async def test_anonymous_cannot_access_github(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/github")
    assert res.status_code == 401


async def test_github_status_disconnected_and_gh_unavailable(
    client: httpx2.AsyncClient, app: FastAPI
) -> None:
    ctx = MockGitHubContext()
    _setup_mock_github(app, ctx)

    # 1. GET /github with nothing connected -> mode: "none"
    res = await client.get("/api/github")
    assert res.status_code == 200
    assert res.json() == {"mode": "none", "login": None, "gh_available": False}

    # 2. POST /github/gh when gh is not available -> 409 gh_not_installed
    res = await client.post("/api/github/gh")
    assert res.status_code == 409
    assert res.json()["code"] == "gh_not_installed"


async def test_github_repos_disconnected_raises_409(
    client: httpx2.AsyncClient, app: FastAPI
) -> None:
    ctx = MockGitHubContext()
    _setup_mock_github(app, ctx)

    res = await client.get("/api/github/repos")
    assert res.status_code == 409
    assert res.json()["code"] == "github_not_connected"


async def test_set_github_token_rejected_does_not_save_or_echo_token(
    client: httpx2.AsyncClient, app: FastAPI
) -> None:
    ctx = MockGitHubContext()
    _setup_mock_github(app, ctx)

    rejected_token = "ghp_bad_secret_token_12345"
    res = await client.post("/api/github/token", json={"token": rejected_token})
    assert res.status_code == 422
    assert res.json()["code"] == "github_token_rejected"
    assert rejected_token not in res.text

    # Nothing saved in keystore or DB
    assert app.state.services.keystore.get(GITHUB_KEYSTORE_REF) is None
    status_res = await client.get("/api/github")
    assert status_res.json()["mode"] == "none"


async def test_set_github_token_success_and_repos_and_delete(
    client: httpx2.AsyncClient, app: FastAPI
) -> None:
    ctx = MockGitHubContext()
    _setup_mock_github(app, ctx)

    token = "ghp_valid"
    res = await client.post("/api/github/token", json={"token": token})
    assert res.status_code == 200
    assert res.json() == {"mode": "token", "login": "octo", "gh_available": False}
    assert token not in res.text

    # Key store holds the token
    assert app.state.services.keystore.get(GITHUB_KEYSTORE_REF) == token

    # GET /github/repos returns repos
    res = await client.get("/api/github/repos")
    assert res.status_code == 200
    repos = res.json()
    assert len(repos) == 1
    assert repos[0] == {
        "full_name": "octo/demo",
        "private": True,
        "default_branch": "main",
        "description": "Octocat Demo",
    }

    # DELETE /github -> 204
    res = await client.delete("/api/github")
    assert res.status_code == 204

    # Status is now "none" and keystore no longer holds token
    status_res = await client.get("/api/github")
    assert status_res.json()["mode"] == "none"
    assert app.state.services.keystore.get(GITHUB_KEYSTORE_REF) is None


async def test_add_github_project_and_refresh_failure(
    client: httpx2.AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    _, bare = _bare_remote(tmp_path, git_env)
    ctx = MockGitHubContext(bare=bare)
    _setup_mock_github(app, ctx)

    # Connect GitHub token first
    token_res = await client.post("/api/github/token", json={"token": "ghp_valid"})
    assert token_res.status_code == 200

    # POST /projects with github source -> 201
    res = await client.post("/api/projects", json={"source": "github", "repo": "octo/demo"})
    assert res.status_code == 201, res.text
    project = res.json()
    project_id: str = project["id"]
    assert project["status"] == "ok"
    assert project["github_full_name"] == "Octo/Demo"

    # POST /projects/{id}/refresh -> ok
    res = await client.post(f"/api/projects/{project_id}/refresh")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Make refresh fail: revoke token and rename bare repo so fetch fails
    ctx.revoked.add("ghp_valid")
    bare.rename(tmp_path / "gone.git")

    res = await client.post(f"/api/projects/{project_id}/refresh")
    assert res.status_code == 200
    refreshed = res.json()
    assert refreshed["status"] == "ok"
    assert refreshed["status_detail"] is not None
    assert refreshed["status_detail"].startswith("Refresh failed: GitHub rejected the token.")
    assert "last downloaded copy" in refreshed["status_detail"].lower()

    # The project keeps its last copy: files are still readable
    proj_obj = await app.state.services.workspace.get(project_id)
    assert (app.state.services.workspace.repo_root(proj_obj) / "README.md").read_text() == "first"
