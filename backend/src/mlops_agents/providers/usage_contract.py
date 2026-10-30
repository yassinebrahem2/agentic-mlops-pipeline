"""Usage contracts and models (I42)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_per_mtok: float
    completion_per_mtok: float
    source: Literal["provider", "user"]


class UsageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    agent: str
    model_address: str
    week_start: date
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None


class UsageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[UsageRow]
    unpriced_models: list[str]
