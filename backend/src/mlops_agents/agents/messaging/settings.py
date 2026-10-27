"""The project's message limits (spec section 7): hops and tokens per exchange."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings

HOPS_KEY = "message_max_hops"
TOKENS_KEY = "message_max_tokens"
DEFAULT_MAX_HOPS = 6
DEFAULT_MAX_TOKENS = 300_000


class MessagingLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_hops: int = Field(ge=1)
    max_tokens: int = Field(ge=1)


class MessagingSettings:
    """Reads and writes the limits in the project's settings. The caller has checked that the
    project exists."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def get(self, project_id: str) -> MessagingLimits:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(project_settings).where(
                            project_settings.c.key.in_([HOPS_KEY, TOKENS_KEY])
                        )
                    )
                )
                .mappings()
                .all()
            )
        stored = {str(row["key"]): int(json.loads(str(row["value"]))) for row in rows}
        return MessagingLimits(
            max_hops=stored.get(HOPS_KEY, DEFAULT_MAX_HOPS),
            max_tokens=stored.get(TOKENS_KEY, DEFAULT_MAX_TOKENS),
        )

    async def set(self, project_id: str, limits: MessagingLimits) -> MessagingLimits:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            for key, value in (
                (HOPS_KEY, limits.max_hops),
                (TOKENS_KEY, limits.max_tokens),
            ):
                encoded = json.dumps(value)
                await conn.execute(
                    sqlite_insert(project_settings)
                    .values(key=key, value=encoded)
                    .on_conflict_do_update(index_elements=["key"], set_={"value": encoded})
                )
        return limits
