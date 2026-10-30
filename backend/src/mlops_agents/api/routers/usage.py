"""Usage route: the tokens and dollars of every project's model calls."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from mlops_agents.api.deps import ServicesDep
from mlops_agents.providers.usage import usage_view
from mlops_agents.providers.usage_contract import UsageView

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageView)
async def get_usage(
    services: ServicesDep, since: Annotated[date | None, Query()] = None
) -> UsageView:
    projects = await services.workspace.list_projects()
    models = await services.providers.list_models()
    return await usage_view(
        services.databases,
        [p.id for p in projects],
        {m.address: m.price for m in models},
        since=since,
    )
