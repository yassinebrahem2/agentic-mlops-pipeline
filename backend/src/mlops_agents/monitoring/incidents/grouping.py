"""Pipeline DAG downstream reachability and active incident grouping."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import RowMapping

from mlops_agents.monitoring.incidents.contract import SignalDraft, cause_key, stages_of

if TYPE_CHECKING:
    from mlops_agents.monitoring.incidents.store import IncidentStore
    from mlops_agents.proposals.payloads import HandoffPayload


def parse_subject_from_cause_key(c_key: str) -> str:
    """Extracts the subject from a collision-resistant cause key formatted as <len>:<subject>:<model>."""
    parts = c_key.split(":", 1)
    if len(parts) == 2 and parts[0].isdigit():
        length = int(parts[0])
        return parts[1][:length]
    return ""


def is_downstream(
    incident_subject: str,
    draft_subject: str,
    payload: HandoffPayload | None,
) -> bool:
    """Determines whether draft_subject is downstream of incident_subject in the pipeline DAG."""
    if payload is None:
        return False
    incident_stages = stages_of(incident_subject, payload)
    draft_stages = stages_of(draft_subject, payload)
    if not incident_stages or not draft_stages:
        return False

    target_stages = set(draft_stages)

    # Build directed graph from payload.edges
    adj: dict[str, list[str]] = {}
    for edge in payload.edges:
        adj.setdefault(edge.from_stage, []).append(edge.to_stage)

    # BFS traversal starting from incident_stages
    visited: set[str] = set()
    queue: list[str] = []

    for s in incident_stages:
        for to_stage in adj.get(s, []):
            if to_stage not in visited:
                visited.add(to_stage)
                queue.append(to_stage)

    while queue:
        current = queue.pop(0)
        if current in target_stages:
            return True
        for next_stage in adj.get(current, []):
            if next_stage not in visited:
                visited.add(next_stage)
                queue.append(next_stage)

    return False


async def find_matching_active_incident(
    store: IncidentStore,
    project_id: str,
    draft: SignalDraft,
    payload: HandoffPayload | None,
    grouping_window_minutes: int,
) -> RowMapping | None:
    """Finds an active incident that matches the draft signal.

    Candidates from ACTIVE_STATUSES:
    - Cause key matches AND latest signal's ran_at is within grouping_window_minutes of draft.ran_at.
    - OR draft subject is downstream of active incident's subject.

    Oldest open incident wins (earliest started_at, then id).
    """
    candidates = await store.get_active_incidents(project_id)
    draft_c_key = cause_key(draft.subject, draft.model)

    for row in candidates:
        inc_id = str(row["id"])
        inc_c_key = str(row["cause_key"])

        # 1. Cause key match and within window
        if inc_c_key == draft_c_key:
            latest_ran_at = await store.get_latest_signal_ran_at(project_id, inc_id)
            if latest_ran_at is not None:
                diff_s = abs((draft.ran_at - latest_ran_at).total_seconds())
                if diff_s <= grouping_window_minutes * 60:
                    return row

        # 2. Downstream reachability
        inc_subject = parse_subject_from_cause_key(inc_c_key)
        if is_downstream(inc_subject, draft.subject, payload):
            return row

    return None


async def find_reopen_candidate(
    store: IncidentStore,
    project_id: str,
    c_key: str,
    draft_ran_at: datetime,
    reopen_window_hours: int,
) -> RowMapping | None:
    """Finds a resolved incident matching cause_key within the reopen window."""
    latest_row = await store.get_latest_incident_by_cause_key(project_id, c_key)
    if latest_row is None:
        return None

    status = str(latest_row["status"])
    if status != "resolved":
        return None

    resolved_at_raw = latest_row["resolved_at"]
    if not resolved_at_raw:
        return None

    from mlops_agents.monitoring.incidents.store import parse_dt

    resolved_at = parse_dt(str(resolved_at_raw))
    diff_s = (draft_ran_at - resolved_at).total_seconds()
    if 0 <= diff_s <= reopen_window_hours * 3600:
        return latest_row

    return None
