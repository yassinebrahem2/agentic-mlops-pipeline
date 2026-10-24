from __future__ import annotations

from sqlalchemy import insert, select

from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.base import StoreBase
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.tables import llm_calls, turns


class UsageStore(StoreBase):
    """The llm_calls table: what each model call of a turn used."""

    @shielded
    async def add_llm_call(
        self,
        project_id: str,
        turn_id: str,
        *,
        agent: str,
        model_address: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
    ) -> None:
        """Insert a record into the llm_calls table."""
        engine = await self._databases.project(project_id)
        call_id = new_id("llm")
        created_at = now_iso()
        async with engine.begin() as conn:
            await conn.execute(
                insert(llm_calls).values(
                    id=call_id,
                    turn_id=turn_id,
                    agent=agent,
                    model_address=model_address,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    created_at=created_at,
                )
            )

    async def last_llm_call_prompt_tokens(
        self, project_id: str, session_id: str
    ) -> tuple[int, str, str | None, str | None] | None:
        """Return (prompt_tokens, created_at, turn_id, trigger_message_id) of last LLM call, or None."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        llm_calls.c.prompt_tokens,
                        llm_calls.c.created_at,
                        llm_calls.c.turn_id,
                        turns.c.trigger_message_id,
                    )
                    .select_from(llm_calls.join(turns, llm_calls.c.turn_id == turns.c.id))
                    .where(turns.c.session_id == session_id, llm_calls.c.prompt_tokens.is_not(None))
                    .order_by(llm_calls.c.created_at.desc(), llm_calls.c.id.desc())
                    .limit(1)
                )
            ).first()

        if row is None or row[0] is None:
            return None
        return (
            int(row[0]),
            str(row[1]),
            str(row[2]) if row[2] is not None else None,
            str(row[3]) if row[3] is not None else None,
        )
