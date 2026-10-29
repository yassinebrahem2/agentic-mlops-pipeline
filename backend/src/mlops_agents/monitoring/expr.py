"""Condition expression language (I23)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Compare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["lt", "le", "gt", "ge", "eq", "ne"]
    field: str
    value: float | str | bool


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_of: list[Condition]


class AnyOf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any_of: list[Condition]


class Not(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_: Condition


Condition = Compare | AllOf | AnyOf | Not
