"""Rule evaluation and limits checking (I23)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.expr import evaluate
from mlops_agents.monitoring.results.contract import ResultQuery
from mlops_agents.monitoring.rules.contract import RuleView
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import hook_runs

if TYPE_CHECKING:
    from mlops_agents.monitoring.results.contract import Results, StoredResult


async def evaluate_rule(
    results: Results,
    project_id: str,
    rule: RuleView,
    now: datetime,
) -> tuple[bool, list[str]]:
    """Evaluate whether rule conditions hold over the results store.

    Returns (passed, evidence_record_ids).
    """
    if not rule.spec.terms:
        return False, []

    evidence: list[str] = []

    for term in rule.spec.terms:
        since = now - timedelta(minutes=term.window_minutes)
        all_rows: list[StoredResult] = []
        offset = 0
        while True:
            page = await results.query(
                project_id,
                ResultQuery(
                    check_id=term.check_id,
                    model=term.model,
                    since=since,
                    limit=500,
                    offset=offset,
                ),
            )
            all_rows.extend(page.rows)
            if not page.truncated or not page.rows:
                break
            offset += len(page.rows)

        # Filter strictly by term.model:
        # if term.model is None: records with model is None
        # if string: records with model == term.model
        if term.model is None:
            records = [r for r in all_rows if r.model is None]
        else:
            records = [r for r in all_rows if r.model == term.model]

        if len(records) < term.min_records:
            return False, []

        term_evidence: list[str] = []
        if term.quantifier == "latest":
            # Newest record is first since ordered ran_at desc
            newest = records[0]
            data = newest.model_dump(mode="python")
            if not evaluate(term.when, data):
                return False, []
            term_evidence.append(newest.id)

        elif term.quantifier == "any":
            for r in records:
                data = r.model_dump(mode="python")
                if evaluate(term.when, data):
                    term_evidence.append(r.id)
            if not term_evidence:
                return False, []

        elif term.quantifier == "all":
            for r in records:
                data = r.model_dump(mode="python")
                if not evaluate(term.when, data):
                    return False, []
                term_evidence.append(r.id)
        else:
            return False, []

        evidence.extend(term_evidence)

    # Deduplicate evidence record IDs preserving order
    deduped = list(dict.fromkeys(evidence))
    return True, deduped


async def rule_limits_allow(
    databases: Databases,
    project_id: str,
    rule: RuleView,
    incident_id: str | None,
    now: datetime,
) -> tuple[bool, str | None]:
    """Check whether hook run limits for a rule allow execution.

    Used by RuleAuthority and Hooks.run.
    Counts hook_runs rows where rule_id == rule.id and basis == 'rule'.
    """
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        if incident_id is not None:
            incident_stmt = (
                select(func.count())
                .select_from(hook_runs)
                .where(
                    hook_runs.c.rule_id == rule.id,
                    hook_runs.c.basis == "rule",
                    hook_runs.c.incident_id == incident_id,
                )
            )
            incident_count = int((await conn.scalar(incident_stmt)) or 0)
            if incident_count >= rule.spec.limits.per_incident:
                return False, f"per_incident limit of {rule.spec.limits.per_incident} reached"

        since_24h = to_iso(now - timedelta(hours=24))
        day_stmt = (
            select(func.count())
            .select_from(hook_runs)
            .where(
                hook_runs.c.rule_id == rule.id,
                hook_runs.c.basis == "rule",
                hook_runs.c.started >= since_24h,
            )
        )
        day_count = int((await conn.scalar(day_stmt)) or 0)
        if day_count >= rule.spec.limits.per_day:
            return False, f"per_day limit of {rule.spec.limits.per_day} reached"

    return True, None
