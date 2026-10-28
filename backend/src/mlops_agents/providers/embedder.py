"""Model types and embedders (I14)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel

ModelType = Literal["chat", "embedder"]


class EmbedderUnavailable(Exception):
    """Every key is exhausted or rate-limited (when not waiting), or the provider is unreachable.
    The message is shown to the user."""


class Embedder(Protocol):
    address: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Raises EmbedderUnavailable."""
        ...


class EmbedderStatus(BaseModel):
    configured: bool
    address: str | None
    dimensions: int | None
    problem: str | None


class Embedders(Protocol):
    """Injected into the services that need it, never looked up."""

    async def for_project(self, project_id: str) -> Embedder | None:
        """The project's override, else the app default, else None."""
        ...

    async def status(self, project_id: str) -> EmbedderStatus: ...
