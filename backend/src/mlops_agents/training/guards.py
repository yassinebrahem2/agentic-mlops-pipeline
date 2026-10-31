"""Training guards: data held, open data quality incidents, and immature labels (Task 2, #487)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mlops_agents.agents.approvals.authority import ActionAuthority, ActionCall
from mlops_agents.monitoring.incidents.contract import ACTIVE_STATUSES
from mlops_agents.training.contract import GuardRefusal, TrainingWindow

if TYPE_CHECKING:
    from mlops_agents.monitoring.incidents.contract import Incidents
    from mlops_agents.proposals.payloads import ModelSpec


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _to_utc_opt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _to_utc(dt)


async def check_data_held(
    incidents: Incidents,
    project_id: str,
    model: str,
    training_tables: set[str],
    window: TrainingWindow,
) -> list[GuardRefusal]:
    """Check for active holds overlapping the training window on training tables."""
    holds = await incidents.holds(project_id, model)
    refusals: list[GuardRefusal] = []

    w_start = _to_utc(window.start)
    w_end = _to_utc(window.end)

    for hold in holds:
        # Empty tables means the whole model is held; otherwise must intersect training_tables
        table_matches = (not hold.tables) or bool(set(hold.tables).intersection(training_tables))
        if not table_matches:
            continue

        h_start = _to_utc_opt(hold.start)
        h_end = _to_utc_opt(hold.end)

        # Overlaps window [window.start, window.end] if (hold.end is None or hold.end > window.start)
        # and (hold.start is None or hold.start < window.end)
        overlaps = (h_end is None or h_end > w_start) and (h_start is None or h_start < w_end)
        if not overlaps:
            continue

        matched_tables = (
            sorted(set(hold.tables).intersection(training_tables))
            if hold.tables
            else ["<all tables>"]
        )
        refusals.append(
            GuardRefusal(
                code="data_held",
                detail=(
                    f"Data hold overlaps window on table(s) {matched_tables} "
                    f"(hold [{hold.start}, {hold.end}] overlaps window [{window.start}, {window.end}])."
                ),
            )
        )

    return refusals


async def check_open_data_quality_incident(
    incidents: Incidents,
    project_id: str,
    training_tables: set[str],
    window: TrainingWindow,
    now: datetime,
) -> list[GuardRefusal]:
    """Check for open data quality incidents whose signals touch training tables."""
    if not training_tables:
        return []

    refusals: list[GuardRefusal] = []
    w_start = _to_utc(window.start)
    w_end = _to_utc(window.end)
    now_utc = _to_utc(now)

    offset = 0
    limit = 100
    while True:
        page = await incidents.list(
            project_id,
            status=list(ACTIVE_STATUSES),
            limit=limit,
            offset=offset,
        )
        for incident in page.rows:
            if incident.status in ("resolved", "closed", "merged"):
                continue
            if incident.category != "data_quality":
                continue

            inc_started = _to_utc(incident.started_at)

            # Span is [incident.started_at, now]. Overlaps window if incident.started_at < window.end and now > window.start
            overlaps = inc_started < w_end and now_utc > w_start
            if not overlaps:
                continue

            signals = await incidents.signals(project_id, incident.id)
            matched_subjects = [s.subject for s in signals if s.subject in training_tables]
            if matched_subjects:
                refusals.append(
                    GuardRefusal(
                        code="open_data_quality_incident",
                        detail=(
                            f"Open data quality incident '{incident.id}' on table(s) "
                            f"{sorted(set(matched_subjects))} overlaps training window."
                        ),
                    )
                )

        offset += len(page.rows)
        if not page.rows or offset >= page.total:
            break

    return refusals


def check_labels_immature(
    model_spec: ModelSpec | None,
    window: TrainingWindow,
    now: datetime,
    model_name: str | None = None,
) -> GuardRefusal | None:
    """Check if training window exceeds the model's label maturity cutoff."""
    if model_spec is None:
        target = f" for model '{model_name}'" if model_name else ""
        return GuardRefusal(
            code="labels_immature",
            detail=f"No approved handoff found{target}.",
        )

    days = model_spec.profile.label_maturity_days
    now_utc = _to_utc(now)
    w_end = _to_utc(window.end)

    cutoff = now_utc - timedelta(days=days)
    if w_end > cutoff:
        return GuardRefusal(
            code="labels_immature",
            detail=(
                f"Window end ({window.end.isoformat()}) exceeds label maturity cutoff "
                f"({cutoff.isoformat()}, {days} days maturity)."
            ),
        )

    return None


async def check_policy_limit(
    authority: ActionAuthority,
    project_id: str,
    model: str,
    policy_id: str | None,
    hook_id: str | None = None,
) -> GuardRefusal | None:
    """Check rate limits and authority requirements through PolicyAuthority."""
    if policy_id is None:
        return None

    call = ActionCall(
        project_id=project_id,
        agent="system",
        kind="train",
        model=model,
        policy_id=policy_id,
        hook_id=hook_id,
    )
    auth = await authority.authorize(call)
    if auth.basis != "policy":
        return GuardRefusal(code="policy_limit", detail=auth.reason)
    return None
