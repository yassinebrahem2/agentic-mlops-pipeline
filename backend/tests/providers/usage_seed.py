"""Seeds llm_calls with chosen timestamps (the runtime store stamps them with now)."""

from __future__ import annotations

from sqlalchemy import insert

from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import llm_calls, sessions, turns


async def seed_calls(
    databases: Databases,
    project_id: str,
    calls: list[tuple[str, str, int | None, int | None, str]],
) -> None:
    """Each call is (agent, model_address, prompt_tokens, completion_tokens, created_at)."""
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        session_id = new_id("ses")
        turn_id = new_id("trn")
        await conn.execute(
            insert(sessions).values(id=session_id, agent="orchestrator", created_at="2026-01-01")
        )
        await conn.execute(
            insert(turns).values(
                id=turn_id,
                session_id=session_id,
                trigger_message_id="msg_x",
                status="done",
                created_at="2026-01-01",
            )
        )
        for agent, address, prompt, completion, created_at in calls:
            await conn.execute(
                insert(llm_calls).values(
                    id=new_id("llm"),
                    turn_id=turn_id,
                    agent=agent,
                    model_address=address,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    latency_ms=1,
                    created_at=created_at,
                )
            )
