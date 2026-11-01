"""StatsService (I41): computed on read, nothing stored. It windows, filters and assembles; the
numbers themselves are `stats/metrics.py`'s."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final, Protocol

from mlops_agents.monitoring.hooks.contract import HookRunView, Hooks
from mlops_agents.monitoring.incidents.contract import Incidents, IncidentView
from mlops_agents.monitoring.results.constants import QUERY_LIMIT_MAX
from mlops_agents.monitoring.results.contract import ResultCategory, ResultQuery, Results
from mlops_agents.monitoring.verdicts.contract import Baseline, Verdicts
from mlops_agents.monitoring.verdicts.store import is_label_evidence
from mlops_agents.providers.models import ModelEntry
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.stats import sources
from mlops_agents.stats.contract import STAT_METRICS, Stats, StatsView, StatsWindow
from mlops_agents.stats.metrics import METRICS, Impact, StatsInputs, impact_candidates
from mlops_agents.stats.sources import Span
from mlops_agents.storage.databases import Databases

_WINDOW_DAYS: Final[dict[StatsWindow, int | None]] = {"7d": 7, "30d": 30, "all": None}
# `Hooks.runs` has no offset (I29): the read asks for more until a call comes back short.
_HOOK_RUNS_FIRST_LIMIT: Final[int] = 200


class ModelLookup(Protocol):
    """The model list's one read Stats needs: a model's current price."""

    async def get_model(self, address: str) -> ModelEntry | None: ...


class StatsService(Stats):
    def __init__(
        self,
        *,
        databases: Databases,
        incidents: Incidents,
        verdicts: Verdicts,
        baseline: Baseline,
        results: Results,
        hooks: Hooks,
        models: ModelLookup,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._verdicts = verdicts
        self._baseline = baseline
        self._results = results
        self._hooks = hooks
        self._models = models

    async def compute(
        self,
        project_id: str,
        window: StatsWindow,
        category: ResultCategory | None,
        now: datetime,
    ) -> StatsView:
        days = _WINDOW_DAYS[window]
        span = Span(start=None if days is None else now - timedelta(days=days), end=now)
        inputs = await self._inputs(project_id, span, category)
        return StatsView(
            window=window,
            category=category,
            baseline_available=inputs.baseline_available,
            groups={
                group: {name: METRICS[name](inputs) for name in names}
                for group, names in STAT_METRICS.items()
            },
        )

    async def _inputs(
        self, project_id: str, span: Span, category: ResultCategory | None
    ) -> StatsInputs:
        opened = [i for i in await self._all_incidents(project_id) if span.contains(i.detected_at)]
        incidents = [i for i in opened if category is None or i.category == category]
        ids = {i.id for i in incidents}
        # A verdict belongs to its incident's window, whenever it was recorded.
        verdicts = {
            v.incident_id: v for v in await self._verdicts.list(project_id) if v.incident_id in ids
        }
        misses = await self._verdicts.list_misses(project_id)
        judged = await self._baseline.judged_silent_alerts(project_id)
        hook_runs = await self._hook_runs(project_id, span)
        tokens_by_model = await sources.tokens_by_model(self._databases, project_id, span)
        db = (self._databases, project_id, span)
        return StatsInputs(
            span=span,
            category=category,
            baseline_available=await self._baseline.available(project_id),
            incidents=incidents,
            verdicts=verdicts,
            matched=await self._baseline.matched_incidents(project_id),
            judged=[a for a in judged if span.contains(a.ran_at)],
            miss_reports=sum(1 for m in misses if span.contains(m.occurred_at)),
            hook_runs=[r for r in hook_runs if span.contains(r.started)],
            impact={
                i.id: await self._impact(project_id, i)
                for i in impact_candidates(incidents, verdicts)
            },
            approvals_created=await sources.approvals_created(*db),
            approvals_approved=await sources.approvals_approved(*db),
            approvals_rejected=await sources.approvals_rejected(*db),
            prs_opened=await sources.prs_opened(*db),
            prs_merged=await sources.prs_merged(*db),
            prs_closed=await sources.prs_closed(*db),
            failed_turns=await sources.failed_turns(*db),
            tool_errors=await sources.tool_errors(*db),
            sandbox_failures=await sources.sandbox_build_failures(*db),
            tokens_by_agent=await sources.tokens_by_agent(*db),
            tokens_by_model=tokens_by_model,
            prices={a: await self._price(a) for a in tokens_by_model},
        )

    async def _all_incidents(self, project_id: str) -> list[IncidentView]:
        """Every incident but the merged ones (they were absorbed)."""
        found: list[IncidentView] = []
        offset = 0
        while True:
            page = await self._incidents.list(project_id, limit=QUERY_LIMIT_MAX, offset=offset)
            found.extend(i for i in page.rows if i.status != "merged")
            if not page.truncated or not page.rows:
                return found
            offset += len(page.rows)

    async def _hook_runs(self, project_id: str, span: Span) -> list[HookRunView]:
        limit = _HOOK_RUNS_FIRST_LIMIT
        while True:
            runs = await self._hooks.runs(project_id, since=span.start, limit=limit)
            if len(runs) < limit:
                return runs
            limit *= 2

    async def _price(self, address: str) -> ModelPrice | None:
        model = await self._models.get_model(address)
        return model.price if model is not None else None

    async def _impact(self, project_id: str, incident: IncidentView) -> Impact:
        detected, resolved = incident.detected_at, incident.resolved_at
        before = await self._latest_label_value(
            project_id,
            ResultQuery(model=incident.model, origin="agent", until=detected),
            lambda at: at < detected,
        )
        after = (
            await self._latest_label_value(
                project_id,
                ResultQuery(model=incident.model, origin="agent", since=resolved),
                lambda at: at > resolved,
            )
            if resolved is not None
            else None
        )
        return Impact(before=before, after=after)

    async def _latest_label_value(
        self, project_id: str, query: ResultQuery, accept: Callable[[datetime], bool]
    ) -> float | None:
        """The newest label-based result value the query finds with an accepted `ran_at`."""
        offset = 0
        while True:
            page = await self._results.query(
                project_id, query.model_copy(update={"limit": QUERY_LIMIT_MAX, "offset": offset})
            )
            for r in page.rows:
                if is_label_evidence(r) and r.value is not None and accept(r.ran_at):
                    return r.value
            if not page.truncated or not page.rows:
                return None
            offset += len(page.rows)
