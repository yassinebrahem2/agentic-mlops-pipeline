"""Tests for connection routes (/api/projects/{id}/connections...)."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from sqlalchemy import insert

from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.storage.tables import projects
from tests.api.conftest import SERVER_ENV, SERVER_URL, signed_in, started_app


@pytest.fixture
async def sample_project(client: httpx2.AsyncClient, tmp_path: Path) -> tuple[str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    res = await client.post(
        "/api/projects",
        json={"source": "local", "path": str(repo), "init_git": True},
    )
    assert res.status_code == 201
    return res.json()["id"], repo


async def test_connections_routes_crud(
    client: httpx2.AsyncClient,
    sample_project: tuple[str, Path],
    tmp_path: Path,
) -> None:
    project_id, _ = sample_project
    data_dir = tmp_path / "my_data"
    data_dir.mkdir()
    (data_dir / "sales.csv").write_text("id,val\n1,10", encoding="utf-8")

    # 1. Inspect
    inspect_res = await client.post(
        f"/api/projects/{project_id}/connections/inspect",
        json={"kind": LOCAL_FOLDER_KIND, "settings": {"path": str(data_dir)}},
    )
    assert inspect_res.status_code == 200
    inspect_data = inspect_res.json()
    assert inspect_data["truncated"] is False
    assert inspect_data["unsupported_files"] == []

    # 2. Create
    create_res = await client.post(
        f"/api/projects/{project_id}/connections",
        json={
            "kind": LOCAL_FOLDER_KIND,
            "name": "sales-data",
            "settings": {"path": str(data_dir)},
        },
    )
    assert create_res.status_code == 201
    con = create_res.json()
    con_id = con["id"]
    assert con["name"] == "sales-data"
    assert con["status"] == "ok"
    assert con["data_version"] is not None

    # 3. Duplicate name -> 409
    dup_res = await client.post(
        f"/api/projects/{project_id}/connections",
        json={
            "kind": LOCAL_FOLDER_KIND,
            "name": "sales-data",
            "settings": {"path": str(data_dir)},
        },
    )
    assert dup_res.status_code == 409
    assert dup_res.json()["code"] == "connection_name_taken"

    # 4. Invalid name -> 400
    bad_name_res = await client.post(
        f"/api/projects/{project_id}/connections",
        json={
            "kind": LOCAL_FOLDER_KIND,
            "name": "BAD NAME",
            "settings": {"path": str(data_dir)},
        },
    )
    assert bad_name_res.status_code == 400
    assert bad_name_res.json()["code"] == "invalid_connection_name"

    # 5. List
    list_res = await client.get(f"/api/projects/{project_id}/connections")
    assert list_res.status_code == 200
    listing = list_res.json()
    assert len(listing) == 1
    assert listing[0]["id"] == con_id

    # 6. Tables
    tables_res = await client.get(f"/api/projects/{project_id}/connections/{con_id}/tables")
    assert tables_res.status_code == 200
    tables = tables_res.json()
    assert len(tables) == 1
    assert tables[0]["name"] == "sales"

    # 7. Update
    data_dir2 = tmp_path / "my_data2"
    data_dir2.mkdir()
    (data_dir2 / "orders.parquet").write_bytes(b"PAR1")

    patch_res = await client.patch(
        f"/api/projects/{project_id}/connections/{con_id}",
        json={"settings": {"path": str(data_dir2)}},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["settings"]["path"] == str(data_dir2)

    # 8. Delete
    del_res = await client.delete(f"/api/projects/{project_id}/connections/{con_id}")
    assert del_res.status_code == 204

    # Post-delete 404
    get_del = await client.get(f"/api/projects/{project_id}/connections/{con_id}/tables")
    assert get_del.status_code == 404


async def test_server_mode_create_and_browse(
    tmp_path: Path,
) -> None:
    async with (
        started_app(tmp_path / "server_data", RunMode.SERVER, SERVER_ENV) as app,
        signed_in(app, SERVER_URL) as server_client,
    ):
        # GET /api/folders answers 200 in server mode
        folders_res = await server_client.get("/api/folders")
        assert folders_res.status_code == 200

        # Create a github project
        # In server mode, local project is disabled, but project can be github
        engine = await app.state.services.databases.app()

        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id="prj_srv1",
                    name="server-proj",
                    source="github",
                    github_full_name="org/srv",
                    github_key="org/srv",
                    default_branch="main",
                    status="ok",
                    created_at=now_iso(),
                )
            )

        data_dir = tmp_path / "server_data_folder"
        data_dir.mkdir()
        (data_dir / "feed.json").write_text("{}", encoding="utf-8")

        # Create connection in server mode works
        con_res = await server_client.post(
            "/api/projects/prj_srv1/connections",
            json={
                "kind": LOCAL_FOLDER_KIND,
                "name": "srv-data",
                "settings": {"path": str(data_dir)},
            },
        )
        assert con_res.status_code == 201
        assert con_res.json()["status"] == "ok"


async def test_project_isolation(
    client: httpx2.AsyncClient,
    tmp_path: Path,
) -> None:
    # Project 1
    r1 = tmp_path / "r1"
    r1.mkdir()
    p1 = (
        await client.post(
            "/api/projects", json={"source": "local", "path": str(r1), "init_git": True}
        )
    ).json()["id"]

    # Project 2
    r2 = tmp_path / "r2"
    r2.mkdir()
    p2 = (
        await client.post(
            "/api/projects", json={"source": "local", "path": str(r2), "init_git": True}
        )
    ).json()["id"]

    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "a.csv").write_text("x", encoding="utf-8")

    await client.post(
        f"/api/projects/{p1}/connections",
        json={"kind": LOCAL_FOLDER_KIND, "name": "p1-data", "settings": {"path": str(d1)}},
    )

    # P2 should see empty connections
    list_p2 = (await client.get(f"/api/projects/{p2}/connections")).json()
    assert len(list_p2) == 0
