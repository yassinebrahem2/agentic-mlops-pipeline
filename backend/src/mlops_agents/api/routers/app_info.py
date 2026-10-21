"""Application information and database backup."""

from __future__ import annotations

import asyncio
import importlib.metadata
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from mlops_agents.api.deps import ServicesDep
from mlops_agents.config import APP_NAME, RunMode
from mlops_agents.storage.backup import snapshot_all

router = APIRouter(tags=["app"])


class AppInfo(BaseModel):
    name: str
    version: str
    mode: Literal["local", "server"]
    data_dir: str


class BackupResult(BaseModel):
    path: str


@router.get("/app", response_model=AppInfo)
async def get_app_info(services: ServicesDep) -> AppInfo:
    return AppInfo(
        name=APP_NAME,
        version=importlib.metadata.version(APP_NAME),
        mode="server" if services.settings.mode is RunMode.SERVER else "local",
        data_dir=str(services.paths.root),
    )


@router.post("/app/backup", response_model=BackupResult)
async def backup_app(services: ServicesDep) -> BackupResult:
    dest = await asyncio.to_thread(snapshot_all, services.paths)
    return BackupResult(path=str(dest))
