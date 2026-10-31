"""Placeholder resolution for action hooks (Task 3, #488)."""

from __future__ import annotations

import math
import re
from datetime import UTC
from typing import TYPE_CHECKING

from mlops_agents.monitoring.hooks.contract import (
    HookRefused,
    HookView,
    MissingPlaceholder,
    PlaceholderResolver,
    RunContext,
)
from mlops_agents.tracking.contract import TrackingQuery

if TYPE_CHECKING:
    from mlops_agents.tracking.contract import Tracking

_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:+-]{1,128}$")


class TrackerPlaceholderResolver(PlaceholderResolver):
    """Resolves declared hook placeholders against a RunContext and Tracking."""

    def __init__(self, tracking: Tracking) -> None:
        self._tracking = tracking

    async def resolve(
        self,
        hook: HookView,
        ctx: RunContext,
        project_id: str | None = None,
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        declared_placeholders = hook.spec.placeholders

        for placeholder in declared_placeholders:
            val = await self._resolve_one(placeholder, hook, ctx, project_id)
            if not _SAFE_VALUE_RE.match(val):
                raise HookRefused(
                    f"Refused placeholder value for {placeholder!r}: invalid format {val!r}"
                )
            resolved[placeholder] = val

        return resolved

    async def _resolve_one(
        self,
        placeholder: str,
        hook: HookView,
        ctx: RunContext,
        project_id: str | None,
    ) -> str:
        if placeholder == "model":
            if not ctx.model:
                raise MissingPlaceholder("model")
            return ctx.model

        if placeholder == "champion_version":
            prod_version: str | None = None
            if project_id is not None:
                query = TrackingQuery(what="models", model=ctx.model)
                page = await self._tracking.query(project_id, query)
                for summary in page.rows:
                    if summary.name == ctx.model:
                        prod_version = summary.production_version
                        break
            elif ctx.champion_version is not None:
                prod_version = ctx.champion_version

            if prod_version is None:
                raise MissingPlaceholder("champion_version")
            return prod_version

        if placeholder == "request_id":
            if ctx.request_id is None:
                raise MissingPlaceholder("request_id")
            return ctx.request_id

        if placeholder == "mode":
            if ctx.mode is None or ctx.mode not in ("train", "evaluate"):
                raise MissingPlaceholder("mode")
            return ctx.mode

        if placeholder == "window_start":
            if ctx.window is None:
                raise MissingPlaceholder("window_start")
            dt = ctx.window.start
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            return dt.isoformat()

        if placeholder == "window_end":
            if ctx.window is None:
                raise MissingPlaceholder("window_end")
            dt = ctx.window.end
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            return dt.isoformat()

        if placeholder == "candidate_version":
            if ctx.candidate_version is None:
                raise MissingPlaceholder("candidate_version")
            return ctx.candidate_version

        if placeholder == "threshold":
            if ctx.threshold is None:
                raise MissingPlaceholder("threshold")
            if not math.isfinite(ctx.threshold):
                raise HookRefused(f"Refused non-finite threshold: {ctx.threshold!r}")
            return str(ctx.threshold)

        raise MissingPlaceholder(placeholder)
