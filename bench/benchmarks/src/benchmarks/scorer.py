"""The benchmark scorer: judges incidents and silent alerts against ground truth."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, cast

from demo_world.answer_key import Record
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.verdicts.contract import (
    SilentVerdict,
    VerdictDraft,
)

from benchmarks.client import BenchClient
from benchmarks.dates import DayStarts, business_date

GRACE_DAYS: Final[int] = 7


@dataclass(frozen=True)
class Problem:
    """One injected problem: Record lines sharing kind, start, end."""

    kind: str
    category: ResultCategory
    start: date
    end: date
    columns: tuple[str | None, ...]


def problems(key: Iterable[Record]) -> list[Problem]:
    """Groups Record lines by kind, start, end; skips every other line kind."""
    groups: dict[tuple[str, date, date], list[Record]] = {}
    for item in key:
        if type(item) is not Record:
            continue
        start_date = date.fromisoformat(item.start)
        end_date = date.fromisoformat(item.end)
        group_key = (item.kind, start_date, end_date)
        groups.setdefault(group_key, []).append(item)

    result: list[Problem] = []
    for (kind, start, end), records in groups.items():
        columns = tuple(dict.fromkeys(r.column for r in records))
        result.append(
            Problem(
                kind=kind,
                category=records[0].category,
                start=start,
                end=end,
                columns=columns,
            )
        )
    return result


@dataclass(frozen=True)
class Score:
    matched: Mapping[str, Problem]
    verdicts: Mapping[str, VerdictDraft]
    misses: Sequence[Problem]
    silent: Mapping[str, SilentVerdict]
    excluded: Sequence[str]


def _rank_candidate(
    item: tuple[Problem, int],
    target_date: date,
    target_category: str,
) -> tuple[int, int, date, int]:
    prob, idx = item
    in_window = prob.start - timedelta(days=1) <= target_date <= prob.end
    cat_match = prob.category == target_category
    return (
        0 if in_window else 1,
        0 if cat_match else 1,
        prob.start,
        idx,
    )


class AlreadyScored(Exception):
    """Raised when the project has already been scored."""


async def score(
    client: BenchClient,
    project_id: str,
    key: Sequence[Record],
    starts: DayStarts,
) -> Score:
    """Scores a project's incidents and silent alerts against an answer key."""
    # 1. Before writing anything: check for existing user verdicts or misses
    raw_verdicts = await client.get(f"/projects/{project_id}/verdicts")
    verdicts_data: list[dict[str, Any]] = (
        cast(list[dict[str, Any]], raw_verdicts) if isinstance(raw_verdicts, list) else []
    )

    raw_misses = await client.get(f"/projects/{project_id}/misses")
    misses_data: list[dict[str, Any]] = (
        cast(list[dict[str, Any]], raw_misses) if isinstance(raw_misses, list) else []
    )

    if any(v.get("source") == "user" for v in verdicts_data):
        raise AlreadyScored("Project already has user verdicts recorded")
    if len(misses_data) > 0:
        raise AlreadyScored("Project already has misses recorded")

    # 2. Extract problems from key
    all_problems = problems(key)

    # 3. Paging incidents
    incidents: list[dict[str, Any]] = []
    limit = 100
    offset = 0
    while True:
        raw_page = await client.get(
            f"/projects/{project_id}/incidents",
            params={"limit": str(limit), "offset": str(offset)},
        )
        page_data: dict[str, Any] = (
            cast(dict[str, Any], raw_page) if isinstance(raw_page, dict) else {}
        )
        raw_rows = page_data.get("rows")
        rows: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], raw_rows) if isinstance(raw_rows, list) else []
        )
        incidents.extend(rows)
        if len(rows) < limit:
            break
        offset += len(rows)

    matched: dict[str, Problem] = {}
    verdicts: dict[str, VerdictDraft] = {}
    excluded: list[str] = []

    # 4. Process incidents
    for inc in incidents:
        inc_id = str(inc["id"])
        if inc.get("status") == "merged" or inc.get("merged_into") is not None:
            excluded.append(inc_id)
            continue

        raw_detected_at = str(inc.get("detected_at", ""))
        detected_at = datetime.fromisoformat(raw_detected_at)
        inc_date = business_date(detected_at, starts)
        inc_category = str(inc.get("category", ""))

        candidates: list[tuple[Problem, int]] = []
        for idx, p in enumerate(all_problems):
            if p.start - timedelta(days=1) <= inc_date <= p.end + timedelta(days=GRACE_DAYS):
                candidates.append((p, idx))

        if not candidates:
            draft = VerdictDraft(value="not_an_issue")
            verdicts[inc_id] = draft
            await client.post(
                f"/projects/{project_id}/incidents/{inc_id}/verdict",
                body=draft.model_dump(mode="json", exclude_none=True),
            )
        else:
            best_problem, _ = min(
                candidates,
                key=lambda item: _rank_candidate(item, inc_date, inc_category),
            )
            matched[inc_id] = best_problem

            if best_problem.category == inc_category:
                draft = VerdictDraft(value="real_issue")
            else:
                draft = VerdictDraft(
                    value="wrong_type",
                    right_category=best_problem.category,
                )
            verdicts[inc_id] = draft
            await client.post(
                f"/projects/{project_id}/incidents/{inc_id}/verdict",
                body=draft.model_dump(mode="json", exclude_none=True),
            )

    # 5. Misses
    caught_problems = set(matched.values())
    misses = [p for p in all_problems if p not in caught_problems]

    starts_map = dict(starts)
    for p in misses:
        if p.start in starts_map:
            occurred_at = starts_map[p.start]
        elif starts and p.start < starts[0][0]:
            occurred_at = starts[0][1]
        else:
            for d, dt in reversed(starts):
                if p.start >= d:
                    occurred_at = dt
                    break
            else:
                occurred_at = starts[0][1] if starts else datetime.now(UTC)

        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)

        await client.post(
            f"/projects/{project_id}/misses",
            body={
                "description": f"Missed problem: {p.kind}",
                "category": p.category,
                "occurred_at": occurred_at.isoformat(),
            },
        )

    # 6. Silent team alerts
    raw_alerts = await client.get(f"/projects/{project_id}/baseline/silent-alerts")
    silent_alerts: list[dict[str, Any]] = (
        cast(list[dict[str, Any]], raw_alerts) if isinstance(raw_alerts, list) else []
    )
    silent: dict[str, SilentVerdict] = {}
    for alert in silent_alerts:
        result_id = str(alert["result_id"])
        raw_ran_at = str(alert.get("ran_at", ""))
        ran_at = datetime.fromisoformat(raw_ran_at)
        alert_date = business_date(ran_at, starts)

        is_real = any(
            p.start - timedelta(days=1) <= alert_date <= p.end + timedelta(days=GRACE_DAYS)
            for p in all_problems
        )
        alert_verdict: SilentVerdict = "real_issue" if is_real else "not_an_issue"
        await client.post(
            f"/projects/{project_id}/baseline/silent-alerts/{result_id}/verdict",
            body={"value": alert_verdict},
        )
        silent[result_id] = alert_verdict

    return Score(
        matched=matched,
        verdicts=verdicts,
        misses=misses,
        silent=silent,
        excluded=excluded,
    )


__all__ = [
    "GRACE_DAYS",
    "AlreadyScored",
    "Problem",
    "Score",
    "problems",
    "score",
]
