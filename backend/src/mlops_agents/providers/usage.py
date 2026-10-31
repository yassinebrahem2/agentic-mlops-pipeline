"""Model prices, the one tokens-to-dollars function, and the usage sum (I42)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

from sqlalchemy import func, select

from mlops_agents.providers.usage_contract import ModelPrice, UsageRow, UsageView
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import llm_calls

_TOKENS_PER_MTOK = 1_000_000


def _per_mtok(per_token: object) -> float | None:
    """OpenRouter publishes USD per token as a string; a price is a finite number >= 0."""
    if not isinstance(per_token, str):
        return None
    try:
        value = float(per_token) * _TOKENS_PER_MTOK
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def provider_price(prompt: object, completion: object) -> ModelPrice | None:
    """The price a provider's model list publishes, or None when either half is unusable."""
    prompt_per_mtok = _per_mtok(prompt)
    completion_per_mtok = _per_mtok(completion)
    if prompt_per_mtok is None or completion_per_mtok is None:
        return None
    return ModelPrice(
        prompt_per_mtok=prompt_per_mtok,
        completion_per_mtok=completion_per_mtok,
        source="provider",
    )


def cost_usd(prompt_tokens: int, completion_tokens: int, price: ModelPrice | None) -> float | None:
    if price is None:
        return None
    return (
        prompt_tokens * price.prompt_per_mtok + completion_tokens * price.completion_per_mtok
    ) / _TOKENS_PER_MTOK


async def usage_view(
    databases: Databases,
    project_ids: Sequence[str],
    prices: Mapping[str, ModelPrice | None],
    *,
    since: date | None,
) -> UsageView:
    """Sums every project's llm_calls by agent, model and week. `prices` maps a model address to
    its current price; an address missing from it (a deleted model) has none."""
    # created_at is UTC ISO text ("...Z"), so SQLite's date() reads it as UTC. 'weekday 0' moves
    # to the next Sunday (or stays on one); six days back is that week's Monday.
    day = func.date(llm_calls.c.created_at)
    week = func.date(llm_calls.c.created_at, "weekday 0", "-6 days")
    query = select(
        llm_calls.c.agent,
        llm_calls.c.model_address,
        week.label("week_start"),
        func.sum(func.coalesce(llm_calls.c.prompt_tokens, 0)).label("prompt_tokens"),
        func.sum(func.coalesce(llm_calls.c.completion_tokens, 0)).label("completion_tokens"),
    ).group_by(llm_calls.c.agent, llm_calls.c.model_address, week)
    if since is not None:
        query = query.where(day >= since.isoformat())

    rows: list[UsageRow] = []
    for project_id in project_ids:
        engine = await databases.project(project_id)
        async with engine.connect() as conn:
            result = (await conn.execute(query)).mappings().all()
        for record in result:
            prompt_tokens = int(record["prompt_tokens"])
            completion_tokens = int(record["completion_tokens"])
            address = str(record["model_address"])
            rows.append(
                UsageRow(
                    project_id=project_id,
                    agent=str(record["agent"]),
                    model_address=address,
                    week_start=date.fromisoformat(str(record["week_start"])),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd(prompt_tokens, completion_tokens, prices.get(address)),
                )
            )
    rows.sort(key=lambda r: (-r.week_start.toordinal(), r.project_id, r.agent, r.model_address))
    unpriced = sorted({r.model_address for r in rows if r.cost_usd is None})
    return UsageView(rows=rows, unpriced_models=unpriced)
