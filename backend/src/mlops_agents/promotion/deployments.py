"""The deployments view (I40) and the query providers for reports and promotions (#500)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

import httpx2
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.clock import to_iso
from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.merge import get_open_pull_labels
from mlops_agents.monitoring.hooks.contract import HookRunView, Hooks
from mlops_agents.monitoring.query import MonitoringQuery, QueryWhat
from mlops_agents.promotion.contract import (
    DecisionRow,
    DeploymentsView,
    EnvironmentRow,
    OpenPipelinePr,
    Promotions,
    PromotionView,
    Reports,
    ReportView,
)
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import reports
from mlops_agents.tracking.contract import (
    DeploymentRow,
    ModelVersionRow,
    Page,
    Tracking,
    TrackingQuery,
)
from mlops_agents.tracking.handoff_alias import resolve_candidate_aliases_fn

logger = logging.getLogger(__name__)

__all__ = [
    "DeploymentsService",
    "PromotionsQueryProvider",
    "ReportsQueryProvider",
]

_TRACKER_LIMIT = 500


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class DeploymentsService:
    """Builds the DeploymentsView (I40): it reads its sources and stores nothing."""

    def __init__(
        self,
        handoffs: Handoffs,
        tracking: Tracking,
        promotions: Promotions,
        hooks: Hooks,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
    ) -> None:
        self._handoffs = handoffs
        self._tracking = tracking
        self._promotions = promotions
        self._hooks = hooks
        self._tasks = tasks
        self._github = github
        self._http = http
        self._handoff_error_logged: set[str] = set()

    async def get_deployments(self, project_id: str) -> DeploymentsView:
        return DeploymentsView(
            environments=await self._environments(project_id),
            decisions=await self._decisions(project_id),
            open_prs=await self._open_prs(project_id),
        )

    async def _environments(self, project_id: str) -> list[EnvironmentRow]:
        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            return []
        rows: list[EnvironmentRow] = []
        for spec in handoff.payload.models:
            page = await self._tracking.query(
                project_id,
                TrackingQuery(what="versions", model=spec.name, limit=_TRACKER_LIMIT),
            )
            versions = [r for r in page.rows if isinstance(r, ModelVersionRow)]
            for alias, environment in spec.environments.items():
                holder = next((v.version for v in versions if alias in v.aliases), None)
                rows.append(
                    EnvironmentRow(
                        model=spec.name, environment=environment, alias=alias, version=holder
                    )
                )
        return rows

    async def _decisions(self, project_id: str) -> list[DecisionRow]:
        decisions = [
            DecisionRow(
                at=_utc(p.applied_at or p.created),
                model=p.model,
                kind="promotion",
                from_version=p.champion_version,
                to_version=p.candidate_version,
                decided_by=p.decided_by,
                # A promotion record names no person.
                decided_by_ref=None,
                report_id=p.report_id,
            )
            for p in await self._promotions.history(project_id)
            if p.status == "applied"
        ]
        decisions += await self._hook_decisions(project_id)
        decisions.sort(key=lambda d: d.at, reverse=True)
        return decisions

    async def _hook_decisions(self, project_id: str) -> list[DecisionRow]:
        runs = await self._hooks.runs(project_id, kinds=["rollback", "fall_back"])
        succeeded = sorted((r for r in runs if r.ok is True), key=lambda r: _utc(r.started))
        if not succeeded:
            return []
        production_aliases = await resolve_candidate_aliases_fn(
            self._handoffs, project_id, self._handoff_error_logged
        )
        page = await self._tracking.query(
            project_id, TrackingQuery(what="deployments", limit=_TRACKER_LIMIT)
        )
        moves = sorted(
            (r for r in page.rows if isinstance(r, DeploymentRow)), key=lambda r: _utc(r.seen_at)
        )
        claimed: set[int] = set()
        decisions: list[DecisionRow] = []
        for run in succeeded:
            decision = _hook_decision(run, moves, claimed, production_aliases)
            if decision is not None:
                decisions.append(decision)
        return decisions

    async def _open_prs(self, project_id: str) -> list[OpenPipelinePr]:
        agent_prs = [
            (t.pr_number, t)
            for t in await self._tasks.open_tasks(project_id)
            if t.pr_number is not None and t.review_kind == "github"
        ]
        if not agent_prs:
            return []
        labels = await self._labels(project_id)
        return [
            OpenPipelinePr(
                number=number, title=t.title, agent=t.agent, labels=labels.get(number, [])
            )
            for number, t in agent_prs
        ]

    async def _labels(self, project_id: str) -> dict[int, list[str]]:
        ref = await self._tasks.repo_ref(project_id)
        if ref is None:
            return {}
        # Labels are decoration: GitHub unreachable or refusing leaves them empty, the rest answers.
        try:
            token = await self._github.token()
            if token is None:
                return {}
            return await get_open_pull_labels(self._http, token, f"{ref.owner}/{ref.repo}")
        except DomainError as exc:
            logger.warning(
                "Open pull request labels for project %s could not be read: %s",
                project_id,
                error_text(exc),
            )
            return {}


def _hook_decision(
    run: HookRunView,
    moves: Sequence[DeploymentRow],
    claimed: set[int],
    production_aliases: Callable[[str], Sequence[str]],
) -> DecisionRow | None:
    """One rollback or fall-back as a decision; its versions are the first production alias
    move of its model at or after the run started that no earlier run has claimed."""
    kind: Literal["rollback", "fall_back"]
    if run.kind == "rollback":
        kind = "rollback"
    elif run.kind == "fall_back":
        kind = "fall_back"
    else:
        return None
    if run.model is None:
        return None
    decided_by: Literal["rule", "person", "agent_bypass"]
    ref: str | None
    if run.basis == "rule":
        decided_by, ref = "rule", run.rule_id
    elif run.basis == "approval":
        decided_by, ref = "person", run.basis_ref
    elif run.basis == "none":
        decided_by, ref = "agent_bypass", None
    else:
        # The authority table lets a rollback or fall-back cite a rule only, never a policy.
        logger.warning(
            "Hook run %s (%s) cites basis %s; it is left out of the deployment decisions.",
            run.id,
            kind,
            run.basis,
        )
        return None

    started = _utc(run.started)
    aliases = production_aliases(run.model)
    index = next(
        (
            i
            for i, move in enumerate(moves)
            if i not in claimed
            and move.model == run.model
            and move.alias in aliases
            and _utc(move.seen_at) >= started
        ),
        None,
    )
    move = None if index is None else moves[index]
    if index is not None:
        claimed.add(index)
    return DecisionRow(
        at=_utc(run.ended or run.started),
        model=run.model,
        kind=kind,
        from_version=None if move is None else move.from_version,
        to_version=None if move is None else move.to_version,
        decided_by=decided_by,
        decided_by_ref=ref,
        report_id=None,
    )


class ReportsQueryProvider:
    """what="reports": pages report IDs in SQL and reads each report through Reports.

    MonitoringQueryTool wraps the whole result as untrusted data, so rows are plain views."""

    what: QueryWhat = "reports"

    def __init__(self, databases: Databases, reports_service: Reports) -> None:
        self._databases = databases
        self._reports = reports_service

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[ReportView]:
        conditions: list[ColumnElement[bool]] = []
        if q.id is not None:
            conditions.append(reports.c.id == q.id)
        if q.model is not None:
            conditions.append(reports.c.model == q.model)
        if q.since is not None:
            conditions.append(reports.c.created >= to_iso(q.since))

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            total = (
                await conn.execute(select(func.count(reports.c.id)).where(*conditions))
            ).scalar() or 0
            ids = (
                (
                    await conn.execute(
                        select(reports.c.id)
                        .where(*conditions)
                        .order_by(reports.c.created.desc(), reports.c.id.desc())
                        .limit(q.limit)
                        .offset(q.offset)
                    )
                )
                .scalars()
                .all()
            )

        views = [
            view
            for report_id in ids
            if (view := await self._reports.get(project_id, str(report_id))) is not None
        ]
        return Page(rows=views, total=total, truncated=(q.offset + len(views)) < total)


class PromotionsQueryProvider:
    """what="promotions": the promotion history, filtered and paged.

    MonitoringQueryTool wraps the whole result as untrusted data, so rows are plain views."""

    what: QueryWhat = "promotions"

    def __init__(self, promotions: Promotions) -> None:
        self._promotions = promotions

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[PromotionView]:
        matching = [
            p
            for p in await self._promotions.history(project_id, model=q.model)
            if (q.id is None or p.id == q.id)
            and (q.status is None or p.status == q.status)
            and (q.since is None or _utc(p.created) >= _utc(q.since))
        ]
        rows = matching[q.offset : q.offset + q.limit]
        return Page(
            rows=rows, total=len(matching), truncated=(q.offset + len(rows)) < len(matching)
        )
