"""One function per metric of `STAT_METRICS` (I41), each over the rows the service gathered.

A metric over no rows is `value=None, n=0`, never 0. Verdict-based numbers are `collecting`
below `STATS_MIN_VERDICTS`; numbers that need the baseline are `None` without one,
never `collecting` either (they are not on their way). Cost per incident is `None` under a
category: tokens carry no category.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean, median
from typing import Final

from mlops_agents.monitoring.hooks.contract import HookRunView
from mlops_agents.monitoring.incidents.contract import ACTIVE_STATUSES, IncidentView
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.verdicts.contract import SilentTeamAlert, VerdictValue, VerdictView
from mlops_agents.providers.usage import cost_usd
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.stats.contract import STATS_MIN_VERDICTS, StatValue
from mlops_agents.stats.sources import Span

_WEEK: Final = timedelta(days=7)
_DIAGNOSED: Final = ("data_quality", "drift")
_AUTONOMOUS_BASES: Final = ("rule", "policy")


@dataclass(frozen=True)
class Impact:
    """The latest label-based result value before detection and after resolution."""

    before: float | None
    after: float | None


@dataclass(frozen=True)
class StatsInputs:
    """Every row a metric reads, already windowed. `incidents` are the in-window, non-merged
    incidents of the category asked."""

    span: Span
    category: ResultCategory | None
    baseline_available: bool
    incidents: Sequence[IncidentView]
    verdicts: Mapping[str, VerdictView]
    matched: set[str]
    judged: Sequence[SilentTeamAlert]
    miss_reports: int
    hook_runs: Sequence[HookRunView]
    impact: Mapping[str, Impact]
    approvals_created: int
    approvals_approved: int
    approvals_rejected: int
    prs_opened: int
    prs_merged: int
    prs_closed: int
    failed_turns: int
    tool_errors: int
    sandbox_failures: int
    tokens_by_agent: Mapping[str, int]
    tokens_by_model: Mapping[str, tuple[int, int]]
    prices: Mapping[str, ModelPrice | None]


def _stat(value: float | None, n: int, *, verdicts: bool = False) -> StatValue:
    return StatValue(
        value=value if n > 0 else None,
        n=n,
        collecting=verdicts and n < STATS_MIN_VERDICTS,
    )


def _needing_baseline(inputs: StatsInputs, stat: StatValue) -> StatValue:
    if inputs.baseline_available:
        return stat
    return stat.model_copy(update={"value": None, "collecting": False})


def _count(n: int) -> StatValue:
    return _stat(n, n)


def _with_verdict(inputs: StatsInputs, *values: VerdictValue) -> list[IncidentView]:
    return [
        i
        for i in inputs.incidents
        if i.id in inputs.verdicts and inputs.verdicts[i.id].value in values
    ]


def _resolved_at(incident: IncidentView) -> datetime | None:
    """When the incident was resolved, or None while it is live: a live incident is not
    finished, whatever `resolved_at` it kept (Decision #565)."""
    return incident.resolved_at if incident.status not in ACTIVE_STATUSES else None


def _seconds(pairs: Iterable[tuple[datetime, datetime]]) -> list[float]:
    return [(later - earlier).total_seconds() for earlier, later in pairs]


def precision(inputs: StatsInputs) -> StatValue:
    n = len(_with_verdict(inputs, "real_issue", "not_an_issue", "wrong_type"))
    real = len(_with_verdict(inputs, "real_issue"))
    return _stat(real / n if n else None, n, verdicts=True)


def caught_baseline_missed(inputs: StatsInputs) -> StatValue:
    n = len(_with_verdict(inputs, "real_issue", "not_an_issue", "wrong_type"))
    missed = [i for i in _with_verdict(inputs, "real_issue") if i.id not in inputs.matched]
    return _needing_baseline(inputs, _stat(len(missed), n, verdicts=True))


def false_alarms_avoided(inputs: StatsInputs) -> StatValue:
    avoided = sum(1 for a in inputs.judged if a.verdict == "not_an_issue")
    return _needing_baseline(inputs, _stat(avoided, len(inputs.judged), verdicts=True))


def misses(inputs: StatsInputs) -> StatValue:
    return _count(inputs.miss_reports + sum(1 for a in inputs.judged if a.verdict == "real_issue"))


def detection_delay_s(inputs: StatsInputs) -> StatValue:
    real = _with_verdict(inputs, "real_issue")
    delays = _seconds((i.started_at, i.detected_at) for i in real)
    return _stat(median(delays) if delays else None, len(delays), verdicts=True)


def time_to_resolution_s(inputs: StatsInputs) -> StatValue:
    real = _with_verdict(inputs, "real_issue")
    resolved = [(i.detected_at, at) for i in real if (at := _resolved_at(i)) is not None]
    times = _seconds(resolved)
    return _stat(median(times) if times else None, len(times), verdicts=True)


def alerts_per_week(inputs: StatsInputs) -> StatValue:
    n = len(inputs.incidents)
    if n == 0:
        return _stat(None, 0)
    if inputs.span.start is not None:
        weeks = (inputs.span.end - inputs.span.start) / _WEEK
    else:
        earliest = min(i.detected_at for i in inputs.incidents)
        weeks = max(1.0, (inputs.span.end - earliest) / _WEEK)
    return _stat(n / weeks, n)


def signals_per_incident(inputs: StatsInputs) -> StatValue:
    counts = [len(i.signal_ids) for i in inputs.incidents]
    return _stat(fmean(counts) if counts else None, len(counts))


def bad_data_vs_drift_correct(inputs: StatsInputs) -> StatValue:
    judged = [
        i for i in _with_verdict(inputs, "real_issue", "wrong_type") if i.category in _DIAGNOSED
    ]
    real = sum(1 for i in judged if inputs.verdicts[i.id].value == "real_issue")
    return _stat(real / len(judged) if judged else None, len(judged), verdicts=True)


def autonomous_share(inputs: StatsInputs) -> StatValue:
    runs = inputs.hook_runs
    autonomous = sum(1 for r in runs if r.basis in _AUTONOMOUS_BASES or r.kind == "hold")
    return _stat(autonomous / len(runs) if runs else None, len(runs))


def approvals_requested(inputs: StatsInputs) -> StatValue:
    return _count(inputs.approvals_created)


def acceptance_rate(inputs: StatsInputs) -> StatValue:
    decided = inputs.approvals_approved + inputs.approvals_rejected
    return _stat(inputs.approvals_approved / decided if decided else None, decided)


def prs_opened(inputs: StatsInputs) -> StatValue:
    return _count(inputs.prs_opened)


def prs_merged(inputs: StatsInputs) -> StatValue:
    return _count(inputs.prs_merged)


def prs_closed(inputs: StatsInputs) -> StatValue:
    return _count(inputs.prs_closed)


def impact_candidates(
    incidents: Iterable[IncidentView], verdicts: Mapping[str, VerdictView]
) -> list[IncidentView]:
    """Resolved real-issue model performance incidents with a model: the ones whose label-based
    results show the model before and after."""
    return [
        i
        for i in incidents
        if i.category == "model_performance"
        and i.model is not None
        and _resolved_at(i) is not None
        and i.id in verdicts
        and verdicts[i.id].value == "real_issue"
    ]


def _mean_or_none(values: Sequence[float]) -> StatValue:
    return _stat(fmean(values) if values else None, len(values), verdicts=True)


def metric_before(inputs: StatsInputs) -> StatValue:
    return _mean_or_none([v.before for v in inputs.impact.values() if v.before is not None])


def metric_after(inputs: StatsInputs) -> StatValue:
    return _mean_or_none([v.after for v in inputs.impact.values() if v.after is not None])


def tokens_per_agent(inputs: StatsInputs) -> StatValue:
    agents = len(inputs.tokens_by_agent)
    total = sum(inputs.tokens_by_agent.values())
    return _stat(total / agents if agents else None, agents)


def _dollars(inputs: StatsInputs) -> float | None:
    """The window's dollars, or None when a model used has no price."""
    total = 0.0
    for address, (prompt, completion) in inputs.tokens_by_model.items():
        cost = cost_usd(prompt, completion, inputs.prices.get(address))
        if cost is None:
            return None
        total += cost
    return total


def cost_per_incident(inputs: StatsInputs) -> StatValue:
    n = len(inputs.incidents)
    # The window's spend is not split by category, so a category has no cost to divide.
    dollars = _dollars(inputs) if n and inputs.category is None else None
    return _stat(dollars / n if dollars is not None else None, n)


def failed_runs(inputs: StatsInputs) -> StatValue:
    return _count(inputs.failed_turns)


def tool_errors(inputs: StatsInputs) -> StatValue:
    return _count(inputs.tool_errors)


def sandbox_failures(inputs: StatsInputs) -> StatValue:
    return _count(inputs.sandbox_failures)


METRICS: Final[Mapping[str, Callable[[StatsInputs], StatValue]]] = {
    f.__name__: f
    for f in (
        precision,
        caught_baseline_missed,
        false_alarms_avoided,
        misses,
        detection_delay_s,
        time_to_resolution_s,
        alerts_per_week,
        signals_per_incident,
        bad_data_vs_drift_correct,
        autonomous_share,
        approvals_requested,
        acceptance_rate,
        prs_opened,
        prs_merged,
        prs_closed,
        metric_before,
        metric_after,
        tokens_per_agent,
        cost_per_incident,
        failed_runs,
        tool_errors,
        sandbox_failures,
    )
}
