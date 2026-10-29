"""The `tracking_summary` context hook: compact summary of tracking state (I18, I16)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.tracking.contract import TrackingQuery

if TYPE_CHECKING:
    from mlops_agents.tracking.contract import Tracking

_log = logging.getLogger(__name__)

TRACKING_SUMMARY_MAX_CHARS: Final[int] = 3000
TRACKING_SUMMARY_HOOK: Final[str] = "tracking_summary"


def _format_age(start: datetime, now: datetime) -> str:
    diff = now - start
    total_seconds = max(0, int(diff.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class TrackingSummaryHook:
    name: str = TRACKING_SUMMARY_HOOK

    def __init__(self, tracking: Tracking | None = None) -> None:
        self._tracking = tracking

    async def render(self, ctx: HookContext) -> str:
        if self._tracking is None:
            return "Tracking summary is unavailable."

        try:
            return await self._do_render(ctx)
        except Exception as exc:
            _log.warning("tracking_summary hook failed: %s", exc)
            return "Tracking summary is unavailable."

    async def _do_render(self, ctx: HookContext) -> str:
        if self._tracking is None:
            return "Tracking summary is unavailable."

        sources = await self._tracking.sources(ctx.project_id)
        if not sources:
            return "No tracking source is connected."

        now = datetime.now(UTC)
        sections: list[str] = []

        # 1. Production model and version
        models_page = await self._tracking.query(
            ctx.project_id, TrackingQuery(what="models", limit=50)
        )
        prod_model_name: str | None = None
        prod_version: str | None = None
        prod_run_key: str | None = None

        if models_page.rows:
            # Pick model with production_version if exists, else first model
            chosen = models_page.rows[0]
            for m in models_page.rows:
                if m.production_version is not None:
                    chosen = m
                    break

            prod_model_name = chosen.name
            if chosen.production_version is not None:
                prod_version = chosen.production_version
                sections.append(
                    f"Production model: {chosen.name} v{chosen.production_version} (production alias)"
                )
            elif chosen.latest_version is not None:
                prod_version = chosen.latest_version
                sections.append(
                    f"Production model: {chosen.name} v{chosen.latest_version} (newest version)"
                )
            else:
                sections.append(f"Production model: {chosen.name} (no versions)")
        else:
            sections.append("Production model: none tracked")

        # 2. Latest metric values for that production run (up to 10 metrics)
        if prod_model_name and prod_version:
            v_page = await self._tracking.query(
                ctx.project_id,
                TrackingQuery(what="versions", model=prod_model_name, version=prod_version),
            )
            if v_page.rows and v_page.rows[0].run_key:
                prod_run_key = v_page.rows[0].run_key
                m_page = await self._tracking.query(
                    ctx.project_id,
                    TrackingQuery(what="metrics", run_key=prod_run_key, latest=True, limit=10),
                )
                if m_page.rows:
                    metrics_str = ", ".join(f"{m.metric}={m.value}" for m in m_page.rows)
                    sections.append(f"Production metrics ({prod_run_key}): {metrics_str}")

        # 3. Last 5 runs (key, status, headline metric if any)
        runs_page = await self._tracking.query(ctx.project_id, TrackingQuery(what="runs", limit=5))
        if runs_page.rows:
            run_lines: list[str] = ["Recent runs:"]
            for r in runs_page.rows:
                headline_metric: str | None = None
                try:
                    rm_page = await self._tracking.query(
                        ctx.project_id,
                        TrackingQuery(what="metrics", run_key=r.key, latest=True, limit=1),
                    )
                    if rm_page.rows:
                        m0 = rm_page.rows[0]
                        headline_metric = f"{m0.metric}={m0.value}"
                except Exception:
                    headline_metric = None

                line = f"  - {r.key} ({r.status})"
                if headline_metric:
                    line += f": {headline_metric}"
                run_lines.append(line)
            sections.append("\n".join(run_lines))

        # 4. Last 5 CI runs (workflow, status, age)
        ci_page = await self._tracking.query(ctx.project_id, TrackingQuery(what="ci_runs", limit=5))
        if ci_page.rows:
            ci_lines: list[str] = ["Recent CI runs:"]
            for c in ci_page.rows:
                age_str = _format_age(c.start, now)
                ci_lines.append(f"  - {c.workflow} ({c.status}, {age_str})")
            sections.append("\n".join(ci_lines))

        # 5. One line per source with last_error or with no sync yet
        source_notes: list[str] = []
        for s in sources:
            if s.last_error:
                source_notes.append(f"Source {s.id} ({s.type}) error: {s.last_error}")
            elif s.last_sync is None:
                source_notes.append(f"Source {s.id} ({s.type}): no sync yet")
        if source_notes:
            sections.append("\n".join(source_notes))

        full_text = "\n\n".join(sections)
        if len(full_text) > TRACKING_SUMMARY_MAX_CHARS:
            trunc_notice = "\n[... truncated ...]"
            full_text = full_text[: TRACKING_SUMMARY_MAX_CHARS - len(trunc_notice)] + trunc_notice

        return full_text
