"""Model management and verification routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.providers.models import ModelCreate, ModelEntry

router = APIRouter(tags=["models"])


class VerifyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str


class SetContextWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    value: int = Field(gt=0)


@router.get("/models", response_model=list[ModelEntry])
async def list_models(services: ServicesDep) -> list[ModelEntry]:
    return await services.providers.list_models()


@router.post("/models", status_code=201, response_model=ModelEntry)
async def add_model(body: ModelCreate, services: ServicesDep) -> ModelEntry:
    return await services.providers.add_model(body)


@router.delete("/models", status_code=204)
async def delete_model(address: Annotated[str, Query()], services: ServicesDep) -> None:
    await services.providers.delete_model(address)


@router.post("/models/verify", response_model=ModelEntry)
async def verify_model(body: VerifyModel, services: ServicesDep) -> ModelEntry:
    return await services.providers.verify_model(body.address)


@router.put("/models/context-window", response_model=ModelEntry)
async def set_context_window(body: SetContextWindow, services: ServicesDep) -> ModelEntry:
    return await services.providers.set_context_window(body.address, body.value)
