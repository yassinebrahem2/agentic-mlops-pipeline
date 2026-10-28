from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
from fastapi import FastAPI
from sqlalchemy import insert

from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode
from mlops_agents.storage.tables import projects
from tests.api.conftest import SERVER_ENV, SERVER_URL, signed_in, started_app
from tests.conftest import make_repo


def _rmtree(path: Path) -> None:
    def make_writable_and_retry(func: Callable[..., Any], target: str, exc: BaseException) -> None:
        del exc
        Path(target).chmod(stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)


async def test_anonymous_cannot_access_projects(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects")
    assert res.status_code == 401


async def test_projects_crud_and_lifecycle(
    client: httpx2.AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "demo", {"README.md": "# Demo\n"}, git_env)

    # 1. Add local repository -> 201
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["source"] == "local"
    assert data["status"] == "ok"
    assert data["name"] == "demo"
    project_id: str = data["id"]
    assert project_id.startswith("prj_")

    # 2. List projects -> contains new project
    res = await client.get("/api/projects")
    assert res.status_code == 200
    listed = res.json()
    assert any(p["id"] == project_id for p in listed)

    # 3. GET by id -> returns project
    res = await client.get(f"/api/projects/{project_id}")
    assert res.status_code == 200
    assert res.json()["id"] == project_id

    # 4. Same path again (different case / trailing slash) -> 409 project_exists
    trailing_path = str(repo) + os.sep
    res = await client.post("/api/projects", json={"source": "local", "path": trailing_path})
    assert res.status_code == 409
    assert res.json()["code"] == "project_exists"

    if os.name == "nt":
        upper_path = str(repo).upper()
        res = await client.post("/api/projects", json={"source": "local", "path": upper_path})
        assert res.status_code == 409
        assert res.json()["code"] == "project_exists"

    # 5. Open project -> updates last_opened_at
    res = await client.post(f"/api/projects/{project_id}/open")
    assert res.status_code == 200
    assert res.json()["last_opened_at"] is not None

    # 6. Refresh after deleting folder -> status "missing"
    _rmtree(repo)
    res = await client.post(f"/api/projects/{project_id}/refresh")
    assert res.status_code == 200
    assert res.json()["status"] == "missing"

    # 7. Relink to a new path -> status "ok"
    new_repo = make_repo(tmp_path / "new_demo", {"README.md": "# New\n"}, git_env)
    res = await client.post(
        f"/api/projects/{project_id}/relink",
        json={"path": str(new_repo)},
    )
    assert res.status_code == 200
    relinked = res.json()
    assert relinked["status"] == "ok"
    assert relinked["local_path"] == str(new_repo.resolve())

    # 8. Relink on a GitHub project -> 409 not_local
    engine = await app.state.services.databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id="prj_ghdemo",
                name="ghdemo",
                source="github",
                github_full_name="org/ghdemo",
                github_key="org/ghdemo",
                default_branch="main",
                status="ok",
                created_at=now_iso(),
            )
        )
    res = await client.post(
        "/api/projects/prj_ghdemo/relink",
        json={"path": str(new_repo)},
    )
    assert res.status_code == 409
    assert res.json()["code"] == "not_local"

    # 9. DELETE -> 204, then GET -> 404 project_not_found, data folder gone, repo untouched
    await app.state.services.databases.project(project_id)
    data_dir = app.state.services.paths.project_dir(project_id)
    assert data_dir.exists()

    res = await client.delete(f"/api/projects/{project_id}")
    assert res.status_code == 204
    assert not data_dir.exists()
    assert new_repo.exists()
    assert (new_repo / "README.md").exists()
    assert (new_repo / ".git").exists()

    res = await client.get(f"/api/projects/{project_id}")
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"


async def test_add_not_a_git_repo_and_init_git(
    client: httpx2.AsyncClient,
    tmp_path: Path,
) -> None:
    plain_folder = tmp_path / "not-git"
    plain_folder.mkdir()

    # Not a git repo -> 422 not_a_git_repo
    res = await client.post(
        "/api/projects",
        json={"source": "local", "path": str(plain_folder)},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "not_a_git_repo"

    # With init_git=True -> 201 status "empty"
    res = await client.post(
        "/api/projects",
        json={"source": "local", "path": str(plain_folder), "init_git": True},
    )
    assert res.status_code == 201
    assert res.json()["status"] == "empty"


async def test_folders_browser(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"a.txt": "hi\n"}, git_env)

    # Without path: lists home folder
    res = await client.get("/api/folders")
    assert res.status_code == 200
    data = res.json()
    assert data["path"] == str(Path.home())
    assert isinstance(data["entries"], list)

    # With repository's parent folder: marks repo is_git_repo True
    res = await client.get("/api/folders", params={"path": str(tmp_path)})
    assert res.status_code == 200
    data = res.json()
    assert data["path"] == str(tmp_path)
    entries = data["entries"]
    repo_entry = next((e for e in entries if e["name"] == repo.name), None)
    assert repo_entry is not None
    assert repo_entry["is_git_repo"] is True


async def test_server_mode_disables_local_projects(
    tmp_path: Path,
) -> None:
    async with (
        started_app(tmp_path / "server_data", RunMode.SERVER, SERVER_ENV) as app,
        signed_in(app, SERVER_URL) as server_client,
    ):
        # Insert a project so ProjectDep resolves for relink
        engine = await app.state.services.databases.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id="prj_srvdemo",
                    name="demo",
                    source="github",
                    github_full_name="org/demo",
                    github_key="org/demo",
                    default_branch="main",
                    status="ok",
                    created_at=now_iso(),
                )
            )

        # POST /projects with source: local -> 403 local_projects_disabled
        res = await server_client.post(
            "/api/projects",
            json={"source": "local", "path": str(tmp_path)},
        )
        assert res.status_code == 403
        assert res.json()["code"] == "local_projects_disabled"

        # GET /folders -> 200 in server mode
        res = await server_client.get("/api/folders")
        assert res.status_code == 200
        assert "entries" in res.json()

        # POST /projects/{id}/relink -> 403 local_projects_disabled
        res = await server_client.post(
            "/api/projects/prj_srvdemo/relink",
            json={"path": str(tmp_path)},
        )
        assert res.status_code == 403
        assert res.json()["code"] == "local_projects_disabled"


async def test_request_validation(
    client: httpx2.AsyncClient,
    tmp_path: Path,
) -> None:
    # Unknown field -> 422 invalid_request
    res = await client.post(
        "/api/projects",
        json={"source": "local", "path": str(tmp_path), "unknown": "value"},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"

    # Missing source -> 422 invalid_request
    res = await client.post(
        "/api/projects",
        json={"path": str(tmp_path)},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
