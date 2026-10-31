"""Table rows as the provider and model records."""

from __future__ import annotations

from sqlalchemy import RowMapping

from mlops_agents.providers.models import KeyHint, ModelEntry, Provider
from mlops_agents.providers.usage_contract import ModelPrice


def provider_from_row(row: RowMapping, keys: list[KeyHint]) -> Provider:
    return Provider(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        base_url=row["base_url"],
        status=row["status"],
        status_detail=row["status_detail"],
        keys=keys,
        cooldown_minutes=row["cooldown_minutes"],
    )


def _price_from_row(row: RowMapping) -> ModelPrice | None:
    # The table's CHECK keeps the three columns all null or all set.
    if row["price_source"] is None:
        return None
    return ModelPrice(
        prompt_per_mtok=row["price_prompt_per_mtok"],
        completion_per_mtok=row["price_completion_per_mtok"],
        source=row["price_source"],
    )


def model_from_row(row: RowMapping) -> ModelEntry:
    return ModelEntry(
        address=row["address"],
        provider_id=row["provider_id"],
        provider_name=row["provider_name"],
        wire_model_id=row["wire_model_id"],
        context_window=row["context_window"],
        tool_calling=bool(row["tool_calling"]),
        streaming=bool(row["streaming"]),
        availability=row["availability"],
        availability_detail=row["availability_detail"],
        verified_at=row["verified_at"],
        model_type=row["model_type"],
        dimensions=row["dimensions"],
        price=_price_from_row(row),
        request_token_limit=row["request_token_limit"],
    )
