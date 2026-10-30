"""Table rows as the provider and model records."""

from __future__ import annotations

from sqlalchemy import RowMapping

from mlops_agents.providers.models import KeyHint, ModelEntry, Provider


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
    )
