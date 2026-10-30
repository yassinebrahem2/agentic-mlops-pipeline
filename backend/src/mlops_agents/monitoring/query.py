"""Monitoring query types and query provider registry (I33)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.tracking.contract import Page

QueryWhat = Literal[
    "results",
    "signals",
    "incidents",
    "expectations",
    "checks",
    "rules",
    "hooks",
    "windows",
    "verdicts",
    "fixes",
    "training_requests",
    "policies",
    "reports",
    "promotions",
]


class MonitoringQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: QueryWhat
    id: str | None = None
    model: str | None = None
    subject: str | None = None
    incident_id: str | None = None
    status: str | None = None
    since: datetime | None = None
    limit: int = Field(default=50, le=200)
    offset: int = 0


class QueryProvider(Protocol):
    what: QueryWhat

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[Any]: ...


class QueryProviders:
    """Registry of query providers, one per what."""

    def __init__(self) -> None:
        self._providers: dict[QueryWhat, QueryProvider] = {}

    def register(self, provider: QueryProvider) -> None:
        if provider.what in self._providers:
            raise ValueError(f"A query provider for {provider.what!r} is already registered.")
        self._providers[provider.what] = provider

    def get(self, what: QueryWhat) -> QueryProvider | None:
        return self._providers.get(what)
