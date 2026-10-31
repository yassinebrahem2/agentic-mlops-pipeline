"""Model management and verification routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.api.deps import ServicesDep
from mlops_agents.providers.models import ModelCreate, ModelEntry
from mlops_agents.providers.usage_contract import ModelPrice

router = APIRouter(tags=["models"])


class VerifyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str


class SetContextWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    value: int


class UserPrice(BaseModel):
    # No `source`: the server marks a price set here as the user's. NaN and infinity are refused
    # because Starlette's JSON parser accepts them.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    prompt_per_mtok: float = Field(ge=0)
    completion_per_mtok: float = Field(ge=0)


class SetPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    price: UserPrice | None


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


@router.put("/models/price", response_model=ModelEntry)
async def set_price(body: SetPrice, services: ServicesDep) -> ModelEntry:
    price = (
        None
        if body.price is None
        else ModelPrice(
            prompt_per_mtok=body.price.prompt_per_mtok,
            completion_per_mtok=body.price.completion_per_mtok,
            source="user",
        )
    )
    return await services.providers.set_price(body.address, price)
