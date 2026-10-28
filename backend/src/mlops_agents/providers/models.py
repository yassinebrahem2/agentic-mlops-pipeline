from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from mlops_agents.credentials.scrub import base_url_problem
from mlops_agents.providers.embedder import ModelType

ProviderKind = Literal["hosted", "self_hosted"]
ProviderStatus = Literal["ok", "needs_attention"]
Availability = Literal["untested", "verified", "needs_attention"]
# Equal to the providers.cooldown_minutes column default in app.db (a test pins the two).
DEFAULT_COOLDOWN_MINUTES = 15


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
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES


_TYPE_LABELS: dict[ModelType, str] = {
    "chat": "a chat",
    "embedder": "an embedder",
}


class ModelCreate(BaseModel):
    provider: str
    wire_model_id: str = Field(min_length=1)
    context_window: int | None = Field(default=None, gt=0)
    model_type: ModelType = "chat"


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
    model_type: ModelType = "chat"
    dimensions: int | None = None

    @computed_field
    @property
    def problem(self) -> str | None:
        """Why an agent cannot use this model, or null when it can."""
        # The one place the usable-model rule lives (#88): agent settings and the UI read it.
        if self.availability == "needs_attention":
            return (
                f"The model {self.address} failed: {self.availability_detail}. "
                "Verify it again in AI providers or choose another model."
            )
        if self.model_type == "embedder":
            if self.availability != "verified" or self.dimensions is None:
                return (
                    f"The embedder {self.address} has not passed verification (an embeddings call). "
                    "Verify it in AI providers or choose another model."
                )
            return None

        if self.availability != "verified" or not (self.tool_calling and self.streaming):
            return (
                f"The model {self.address} has not passed verification (tool calls and streaming). "
                "Verify it in AI providers or choose another model."
            )
        if self.context_window is None:
            return f"The context window of {self.address} is unknown. Enter it in AI providers."
        return None

    def problem_for(self, wanted: ModelType) -> str | None:
        if self.model_type != wanted:
            self_lbl = _TYPE_LABELS[self.model_type]
            wanted_lbl = _TYPE_LABELS[wanted]
            return f"{self.address} is {self_lbl} model, not {wanted_lbl} model. Choose {wanted_lbl} model."
        return self.problem


class RemoteModel(BaseModel):
    id: str
    context_window: int | None


class DetectedKey(BaseModel):
    env_var: str
    provider_name: str
    base_url: str
    hint: str
    provider_exists: bool
