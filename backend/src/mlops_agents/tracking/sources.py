"""Tracking sources: validation, row persistence, and credential state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import RowMapping, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from mlops_agents.credentials.project_secrets import ProjectSecrets, tracking_ref
from mlops_agents.errors import DomainError, validation_error_message
from mlops_agents.storage.tables import tracking_sources
from mlops_agents.tracking.contract import (
    GithubActionsSettings,
    MlflowSettings,
    SourceType,
    TrackingSourceView,
)


def validate_source_input(
    type_: str,
    settings: dict[str, Any],
    auth: str,
) -> tuple[SourceType, dict[str, Any], Literal["none", "credential"]]:
    """Validates source type, settings model, and auth mode."""
    if type_ not in ("mlflow", "github_actions"):
        raise DomainError(
            "tracking_bad_source",
            f"Unknown tracking source type: {type_!r}. Must be 'mlflow' or 'github_actions'.",
            status=422,
        )
    source_type: SourceType = type_  # type: ignore[assignment]

    if auth not in ("none", "credential"):
        raise DomainError(
            "tracking_bad_source",
            f"Invalid auth mode: {auth!r}. Must be 'none' or 'credential'.",
            status=422,
        )
    auth_mode: Literal["none", "credential"] = auth  # type: ignore[assignment]

    if source_type == "github_actions" and auth_mode != "none":
        raise DomainError(
            "tracking_bad_source",
            "GitHub Actions tracking uses the app's GitHub login and accepts only auth='none'.",
            status=422,
        )

    try:
        if source_type == "mlflow":
            validated_settings = MlflowSettings.model_validate(settings).model_dump()
        else:
            validated_settings = GithubActionsSettings.model_validate(settings).model_dump()
    except ValidationError as err:
        raise DomainError(
            "tracking_bad_settings", validation_error_message(err).text, status=422
        ) from err

    return source_type, validated_settings, auth_mode


async def row_to_source_view(
    row: RowMapping,
    project_id: str,
    secrets: ProjectSecrets,
) -> TrackingSourceView:
    source_id = str(row["id"])
    auth = str(row["auth"])
    credential_set = (
        await secrets.is_set(tracking_ref(project_id, source_id)) if auth == "credential" else False
    )
    last_sync_raw = row["last_sync"]
    last_sync = datetime.fromisoformat(last_sync_raw) if last_sync_raw else None

    return TrackingSourceView(
        id=source_id,
        type=row["type"],
        settings=json.loads(row["settings"]),
        auth=auth,  # type: ignore[arg-type]
        credential_set=credential_set,
        last_sync=last_sync,
        last_error=row["last_error"],
    )


async def load_source_row(conn: AsyncConnection, source_id: str) -> RowMapping | None:
    stmt = select(tracking_sources).where(tracking_sources.c.id == source_id)
    result = await conn.execute(stmt)
    return result.mappings().one_or_none()


async def load_all_source_rows(
    conn: AsyncConnection,
) -> Sequence[RowMapping]:
    stmt = select(tracking_sources).order_by(tracking_sources.c.created_at.asc())
    result = await conn.execute(stmt)
    return result.mappings().all()


async def insert_source_row(
    conn: AsyncConnection,
    *,
    source_id: str,
    type_: SourceType,
    settings: dict[str, Any],
    auth: Literal["none", "credential"],
    created_at: str,
) -> None:
    stmt = insert(tracking_sources).values(
        id=source_id,
        type=type_,
        settings=json.dumps(settings),
        auth=auth,
        last_sync=None,
        last_error=None,
        created_at=created_at,
    )
    await conn.execute(stmt)
