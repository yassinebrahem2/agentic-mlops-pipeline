from __future__ import annotations

import importlib.metadata
from pathlib import Path

import httpx2
from fastapi import FastAPI

from mlops_agents.config import APP_NAME


async def test_anonymous_cannot_access_app_info(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/app")
    assert res.status_code == 401


async def test_app_info_and_backup(client: httpx2.AsyncClient, app: FastAPI) -> None:
    res = await client.get("/api/app")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == APP_NAME
    assert data["version"] == importlib.metadata.version(APP_NAME)
    assert data["mode"] == "local"
    assert data["data_dir"] == str(app.state.services.paths.root)

    res = await client.post("/api/app/backup")
    assert res.status_code == 200
    backup_path = Path(res.json()["path"])
    assert (backup_path / "app.db").exists()


async def test_app_info_server_mode(server_client: httpx2.AsyncClient) -> None:
    res = await server_client.get("/api/app")
    assert res.status_code == 200
    assert res.json()["mode"] == "server"
