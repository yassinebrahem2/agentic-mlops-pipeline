from typing import Literal

from pydantic import BaseModel, Field, field_validator

from mlops_agents.credentials.scrub import base_url_problem

ProviderKind = Literal["hosted", "self_hosted"]
ProviderStatus = Literal["ok", "needs_attention"]
Availability = Literal["untested", "verified", "needs_attention"]


class ProviderCreate(BaseModel):
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,39}$",
        description="Lowercase letters, digits and dashes; used in model addresses.",
    )
    kind: ProviderKind
    base_url: str

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        problem = base_url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class KeyHint(BaseModel):
    position: int
    hint: str


class Provider(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    status: ProviderStatus
    status_detail: str | None
    keys: list[KeyHint]


class ModelCreate(BaseModel):
    provider: str
    wire_model_id: str = Field(min_length=1)
    context_window: int | None = Field(default=None, gt=0)


class ModelEntry(BaseModel):
    address: str
    provider_id: str
    provider_name: str
    wire_model_id: str
    context_window: int | None
    tool_calling: bool
    streaming: bool
    availability: Availability
    availability_detail: str | None
    verified_at: str | None


class RemoteModel(BaseModel):
    id: str
    context_window: int | None


class DetectedKey(BaseModel):
    env_var: str
    provider_name: str
    base_url: str
    hint: str
    provider_exists: bool
