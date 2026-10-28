"""AI providers and environment keys routes."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.providers.models import (
    DetectedKey,
    Provider,
    ProviderCreate,
    RemoteModel,
)

router = APIRouter(tags=["providers"])


class AddProviderKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(repr=False)


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cooldown_minutes: int = Field(
        ge=1, le=1440, description="Minutes an exhausted key rests before it is tried again."
    )


@router.get("/providers", response_model=list[Provider])
async def list_providers(services: ServicesDep) -> list[Provider]:
    return await services.providers.list_providers()


@router.post("/providers", status_code=201, response_model=Provider)
async def create_provider(body: ProviderCreate, services: ServicesDep) -> Provider:
    return await services.providers.create_provider(body)


@router.delete("/providers/{name}", status_code=204)
async def delete_provider(name: str, services: ServicesDep) -> None:
    await services.providers.delete_provider(name)


@router.put("/providers/{name}/settings", response_model=Provider)
async def update_provider_settings(
    name: str, body: ProviderSettings, services: ServicesDep
) -> Provider:
    return await services.providers.set_cooldown(name, body.cooldown_minutes)


@router.post("/providers/{name}/keys", response_model=Provider)
async def add_provider_key(name: str, body: AddProviderKey, services: ServicesDep) -> Provider:
    return await services.providers.add_key(name, body.key)


@router.delete("/providers/{name}/keys/{position}", response_model=Provider)
async def remove_provider_key(name: str, position: int, services: ServicesDep) -> Provider:
    return await services.providers.remove_key(name, position)


@router.get("/providers/{name}/remote-models", response_model=list[RemoteModel])
async def list_remote_models(name: str, services: ServicesDep) -> list[RemoteModel]:
    return await services.providers.list_remote_models(name)


@router.get("/env-keys", response_model=list[DetectedKey])
async def list_env_keys(services: ServicesDep) -> list[DetectedKey]:
    return await services.providers.detect_env_keys()


@router.post("/env-keys/{env_var}/import", response_model=Provider)
async def import_env_key(env_var: str, services: ServicesDep) -> Provider:
    return await services.providers.import_env_key(env_var)
