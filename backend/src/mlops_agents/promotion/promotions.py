"""SqlitePromotions service and promotion rejection observer (Task 3, #497)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select, update

from mlops_agents.agents.approvals.contract import ApprovalObserver, ApprovalView
from mlops_agents.clock import now_iso, to_iso, utc_now
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import Authority, HookCall, Hooks
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.promotion.contract import (
    EvaluationReader,
    PromotionPayload,
    Promotions,
    PromotionView,
    Reports,
)
from mlops_agents.promotion.evaluation import comparable
from mlops_agents.proposals.contract import PROMOTION_KIND, ApplyResult, Handoffs
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import promotions, proposals
from mlops_agents.tracking.contract import ModelSummary, RunRow, Tracking, TrackingQuery

if TYPE_CHECKING:
    from mlops_agents.proposals.service import ProposalsService
    from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

__all__ = ["PromotionRejectObserver", "SqlitePromotions"]


class SqlitePromotions(Promotions):
    """Sqlite-backed promotions service handling validation, history and hook application."""

    def __init__(
        self,
        databases: Databases,
        tracking: Tracking,
        handoffs: Handoffs,
        reader: EvaluationReader,
        reports: Reports,
        hooks: Hooks,
        events: EventLog,
        clock: Any | None = None,
    ) -> None:
        self._databases = databases
        self._tracking = tracking
        self._handoffs = handoffs
        self._reader = reader
        self._reports = reports
        self._hooks = hooks
        self._events = events
        self._clock = clock

    async def validate(self, project_id: str, payload: PromotionPayload) -> list[str]:
        """Validate candidate promotion before proposal or application.

        Returns a list of user-facing refusal lines (empty if valid).
        """
        refusals: list[str] = []

        # 1. The report exists and is for this model and candidate
        report = await self._reports.get(project_id, payload.report_id)
        if report is None:
            refusals.append(f"Report '{payload.report_id}' does not exist.")
        elif (
            report.draft.model != payload.model
            or report.draft.candidate_version != payload.candidate_version
        ):
            refusals.append(
                f"Report '{payload.report_id}' is for model '{report.draft.model}' version "
                f"'{report.draft.candidate_version}', not '{payload.model}' version '{payload.candidate_version}'."
            )

        # 2. The model's current handoff says promotion: "person"
        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            refusals.append("No active handoff for the project.")
        else:
            model_spec = next((m for m in handoff.payload.models if m.name == payload.model), None)
            if model_spec is None:
                refusals.append(f"Model '{payload.model}' not found in current handoff.")
            elif model_spec.promotion != "person":
                refusals.append(
                    f"Model '{payload.model}' promotion mode is '{model_spec.promotion}', expected 'person'."
                )

        # 3. The deploy hook exists, is of kind 'deploy', belongs to this model and is 'active'
        hook = await self._hooks.get(project_id, payload.deploy_hook_id)
        if hook is None:
            refusals.append(f"Deploy hook '{payload.deploy_hook_id}' does not exist.")
        else:
            if hook.spec.kind != "deploy":
                refusals.append(
                    f"Hook '{payload.deploy_hook_id}' is of kind '{hook.spec.kind}', expected 'deploy'."
                )
            if hook.model != payload.model:
                refusals.append(
                    f"Hook '{payload.deploy_hook_id}' belongs to model '{hook.model}', expected '{payload.model}'."
                )
            if hook.status != "active":
                refusals.append(
                    f"Hook '{payload.deploy_hook_id}' status is '{hook.status}', expected 'active'."
                )

        # 4. The candidate has an evaluation
        candidate_eval = await self._reader.for_candidate(
            project_id, payload.model, payload.candidate_version
        )
        if candidate_eval is None:
            refusals.append(
                f"Candidate version '{payload.candidate_version}' of model '{payload.model}' has no evaluation record."
            )

        # 5. It is fresh (end of run within evaluation_max_age_hours; no end known: not fresh)
        if candidate_eval is not None:
            run_key = candidate_eval.record.run_key
            runs_page = await self._tracking.query(
                project_id, TrackingQuery(what="runs", run_key=run_key)
            )
            run_row = next(
                (r for r in runs_page.rows if isinstance(r, RunRow) and r.key == run_key),
                None,
            )
            if run_row is None or run_row.end is None:
                refusals.append(
                    f"Evaluation run '{run_key}' has no known end time; evaluation is not fresh."
                )
            else:
                settings = await get_monitoring_settings(self._databases, project_id)
                max_age_hours = settings.evaluation_max_age_hours
                now = (
                    self._clock.now()
                    if (self._clock is not None and hasattr(self._clock, "now"))
                    else utc_now()
                )
                run_end = run_row.end
                if run_end.tzinfo is None:
                    run_end = run_end.replace(tzinfo=UTC)
                if now.tzinfo is None:
                    now = now.replace(tzinfo=UTC)
                if (now - run_end).total_seconds() > max_age_hours * 3600:
                    refusals.append(
                        f"Evaluation run ended at {run_end.isoformat()}, exceeding maximum age of {max_age_hours} hours."
                    )

        # 6. Payload champion_version equals current production version and record champion_version
        models_page = await self._tracking.query(
            project_id, TrackingQuery(what="models", model=payload.model)
        )
        model_summary = next(
            (
                s
                for s in models_page.rows
                if isinstance(s, ModelSummary) and s.name == payload.model
            ),
            None,
        )
        current_prod_version = (
            model_summary.production_version if model_summary is not None else None
        )
        rec_champion_version = (
            candidate_eval.payload.champion_version if candidate_eval is not None else None
        )

        if payload.champion_version != current_prod_version:
            refusals.append(
                f"Payload champion version '{payload.champion_version}' does not match current production version '{current_prod_version}'."
            )
        if candidate_eval is not None and payload.champion_version != rec_champion_version:
            refusals.append(
                f"Payload champion version '{payload.champion_version}' does not match evaluation record champion version '{rec_champion_version}'."
            )

        # 7. When there is a champion, its evaluation record exists and is comparable
        if payload.champion_version is not None:
            champ_eval = await self._reader.for_candidate(
                project_id, payload.model, payload.champion_version
            )
            if champ_eval is None:
                refusals.append(
                    f"Champion version '{payload.champion_version}' has no evaluation record."
                )
            elif candidate_eval is not None and not comparable(
                candidate_eval.payload, champ_eval.payload
            ):
                refusals.append(
                    "Champion evaluation record is not comparable with candidate evaluation record."
                )

        return refusals

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]:
        """Return promotion records history, newest first."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(promotions)
            if model is not None:
                stmt = stmt.where(promotions.c.model == model)
            stmt = stmt.order_by(promotions.c.created.desc(), promotions.c.id.desc())
            rows = (await conn.execute(stmt)).mappings().all()

        return [self._row_to_view(row) for row in rows]

    async def apply(
        self,
        project_id: str,
        proposal_id: str,
        payload: PromotionPayload,
    ) -> ApplyResult:
        """Idempotent application of a promotion proposal."""
        engine = await self._databases.project(project_id)

        # Look up proposal to determine decided_by
        async with engine.connect() as conn:
            prop_row = (
                await conn.execute(
                    select(proposals.c.approval_id).where(proposals.c.id == proposal_id)
                )
            ).first()

        decided_by = (
            "agent_bypass" if (prop_row is None or prop_row.approval_id is None) else "person"
        )

        # Look up existing promotion by proposal_id
        async with engine.connect() as conn:
            promo_row = (
                (
                    await conn.execute(
                        select(promotions).where(promotions.c.proposal_id == proposal_id)
                    )
                )
                .mappings()
                .first()
            )

        if promo_row is not None:
            status = str(promo_row["status"])
            if status == "applied":
                return ApplyResult(applied=["promotion"], failed={})
            if status in ("refused", "rejected"):
                raw_refusals = promo_row["refusals"]
                ref_list: list[str] = (
                    [str(x) for x in json.loads(str(raw_refusals))] if raw_refusals else []
                )
                reason = "; ".join(ref_list) if ref_list else f"Promotion {status}"
                return ApplyResult(applied=[], failed={"promotion": reason})
            if status == "proposed":
                # Unfinished: crash during the run. Mark refused.
                refusal_msg = "interrupted; check the Deployments page"
                async with engine.begin() as conn:
                    await conn.execute(
                        update(promotions)
                        .where(promotions.c.id == promo_row["id"])
                        .values(
                            status="refused",
                            refusals=json.dumps([refusal_msg]),
                        )
                    )
                await self._events.record(
                    project_id,
                    source="system",
                    type="promotion.refused",
                    payload={
                        "promotion_id": str(promo_row["id"]),
                        "model": payload.model,
                        "candidate_version": payload.candidate_version,
                        "refusals": [refusal_msg],
                    },
                    subject_id=str(promo_row["id"]),
                )
                return ApplyResult(applied=[], failed={"promotion": refusal_msg})

        # No row exists yet: write as 'proposed' first
        promo_id = new_id("prm")
        now_str = now_iso()
        async with engine.begin() as conn:
            await conn.execute(
                promotions.insert().values(
                    id=promo_id,
                    model=payload.model,
                    candidate_version=payload.candidate_version,
                    champion_version=payload.champion_version,
                    mode="person",
                    decided_by=decided_by,
                    status="proposed",
                    report_id=payload.report_id,
                    refusals="[]",
                    created=now_str,
                    applied_at=None,
                    proposal_id=proposal_id,
                )
            )

        # Re-validate
        refusals = await self.validate(project_id, payload)
        if refusals:
            async with engine.begin() as conn:
                await conn.execute(
                    update(promotions)
                    .where(promotions.c.id == promo_id)
                    .values(
                        status="refused",
                        refusals=json.dumps(refusals),
                    )
                )
            await self._events.record(
                project_id,
                source="system",
                type="promotion.refused",
                payload={
                    "promotion_id": promo_id,
                    "model": payload.model,
                    "candidate_version": payload.candidate_version,
                    "refusals": refusals,
                },
                subject_id=promo_id,
            )
            return ApplyResult(applied=[], failed={"promotion": "; ".join(refusals)})

        # Run deploy hook
        hook_call = HookCall(
            hook_id=payload.deploy_hook_id,
            reason=f"Promotion proposal {proposal_id} approved",
            params={"candidate_version": payload.candidate_version},
        )
        authority = Authority(
            basis="approval",
            ref=proposal_id,
            reason="Promotion proposal approved",
            evidence=[],
        )

        try:
            hook_res = await self._hooks.run(project_id, hook_call, authority)
        except Exception as exc:
            logger.exception("Failed to run deploy hook %s", payload.deploy_hook_id)
            hook_res = None
            hook_detail = str(exc)
        else:
            hook_detail = hook_res.detail

        if hook_res is not None and hook_res.ok:
            applied_at_str = now_iso()
            async with engine.begin() as conn:
                await conn.execute(
                    update(promotions)
                    .where(promotions.c.id == promo_id)
                    .values(
                        status="applied",
                        applied_at=applied_at_str,
                    )
                )
            await self._events.record(
                project_id,
                source="system",
                type="promotion.applied",
                payload={
                    "promotion_id": promo_id,
                    "model": payload.model,
                    "candidate_version": payload.candidate_version,
                    "champion_version": payload.champion_version,
                    "hook_id": payload.deploy_hook_id,
                },
                subject_id=promo_id,
            )
            return ApplyResult(applied=["promotion"], failed={})
        else:
            async with engine.begin() as conn:
                await conn.execute(
                    update(promotions)
                    .where(promotions.c.id == promo_id)
                    .values(
                        status="refused",
                        refusals=json.dumps([hook_detail]),
                    )
                )
            await self._events.record(
                project_id,
                source="system",
                type="promotion.refused",
                payload={
                    "promotion_id": promo_id,
                    "model": payload.model,
                    "candidate_version": payload.candidate_version,
                    "refusals": [hook_detail],
                },
                subject_id=promo_id,
            )
            return ApplyResult(applied=[], failed={"promotion": hook_detail})

    async def record_team_gate(
        self,
        project_id: str,
        *,
        model: str,
        candidate_version: str,
        champion_version: str | None,
        decision: Literal["promote", "hold", "reject"],
        reasons: list[str] | None,
        report_id: str | None,
        created: datetime | None = None,
    ) -> PromotionView | None:
        """Record a team_gate promotion row and emit the matching event.

        Idempotent: returns None if a team_gate promotion row already exists for
        (model, candidate_version).
        """
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            existing = (
                await conn.execute(
                    select(promotions).where(
                        promotions.c.model == model,
                        promotions.c.candidate_version == candidate_version,
                        promotions.c.mode == "team_gate",
                    )
                )
            ).first()
            if existing is not None:
                return None

        promo_id = new_id("prm")
        now_dt = created or (
            self._clock.now()
            if (self._clock is not None and hasattr(self._clock, "now"))
            else utc_now()
        )
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        created_str = to_iso(now_dt)

        if decision == "promote":
            status = "applied"
            applied_at_str: str | None = created_str
            refusals_list: list[str] = []
        else:
            status = "refused"
            applied_at_str = None
            refusals_list = list(reasons) if reasons is not None else []

        refusals_json = json.dumps(refusals_list)

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    promotions.insert().values(
                        id=promo_id,
                        model=model,
                        candidate_version=candidate_version,
                        champion_version=champion_version,
                        mode="team_gate",
                        decided_by="team_gate",
                        status=status,
                        report_id=report_id,
                        refusals=refusals_json,
                        created=created_str,
                        applied_at=applied_at_str,
                        proposal_id=None,
                    )
                )
        except Exception:
            logger.debug(
                "team_gate promotion row for (%s, %s) already inserted concurrently",
                model,
                candidate_version,
            )
            return None

        # Emit event
        if decision == "promote":
            await self._events.record(
                project_id,
                source="system",
                type="promotion.applied",
                payload={
                    "promotion_id": promo_id,
                    "model": model,
                    "candidate_version": candidate_version,
                    "champion_version": champion_version,
                },
                subject_id=promo_id,
            )
        else:
            await self._events.record(
                project_id,
                source="system",
                type="promotion.refused",
                payload={
                    "promotion_id": promo_id,
                    "model": model,
                    "candidate_version": candidate_version,
                    "refusals": refusals_list,
                },
                subject_id=promo_id,
            )

        return PromotionView(
            id=promo_id,
            model=model,
            candidate_version=candidate_version,
            champion_version=champion_version,
            mode="team_gate",
            decided_by="team_gate",
            status=status,
            report_id=report_id,
            refusals=refusals_list,
            created=now_dt,
            applied_at=now_dt if applied_at_str is not None else None,
        )

    async def record_rejection(
        self,
        project_id: str,
        proposal_id: str,
        payload: PromotionPayload,
    ) -> None:
        """Record a person's rejection of a promotion proposal."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(promotions.c.id).where(promotions.c.proposal_id == proposal_id)
                )
            ).first()
            if existing is not None:
                return

            promo_id = new_id("prm")
            now_str = now_iso()
            await conn.execute(
                promotions.insert().values(
                    id=promo_id,
                    model=payload.model,
                    candidate_version=payload.candidate_version,
                    champion_version=payload.champion_version,
                    mode="person",
                    decided_by="person",
                    status="rejected",
                    report_id=payload.report_id,
                    refusals="[]",
                    created=now_str,
                    applied_at=None,
                    proposal_id=proposal_id,
                )
            )

    def _row_to_view(self, row: Any) -> PromotionView:
        refusals_raw = row["refusals"]
        refusals: list[str] = (
            [str(x) for x in json.loads(str(refusals_raw))]
            if refusals_raw and isinstance(refusals_raw, str)
            else []
        )
        return PromotionView(
            id=str(row["id"]),
            model=str(row["model"]),
            candidate_version=str(row["candidate_version"]),
            champion_version=row["champion_version"],
            mode=row["mode"],
            decided_by=row["decided_by"],
            status=row["status"],
            report_id=row["report_id"],
            refusals=refusals,
            created=datetime.fromisoformat(str(row["created"])),
            applied_at=(
                datetime.fromisoformat(str(row["applied_at"])) if row["applied_at"] else None
            ),
        )


class PromotionRejectObserver(ApprovalObserver):
    """Watches approval decisions and records rejected promotion proposals in project.db."""

    def __init__(
        self,
        promotions: SqlitePromotions,
        proposals: ProposalsService,
        workspace: WorkspaceService,
    ) -> None:
        self._promotions = promotions
        self._proposals = proposals
        self._workspace = workspace

    async def asked(self, view: ApprovalView) -> None:
        pass

    async def decided(self, view: ApprovalView) -> None:
        if view.proposal_kind != PROMOTION_KIND or view.status != "rejected":
            return

        for project in await self._workspace.list_projects():
            proposal = await self._proposals.get_by_approval(project.id, view.id)
            if proposal is not None:
                payload = (
                    proposal.payload
                    if isinstance(proposal.payload, PromotionPayload)
                    else PromotionPayload.model_validate(proposal.payload)
                )
                await self._promotions.record_rejection(
                    project.id,
                    proposal.id,
                    payload,
                )
                break
