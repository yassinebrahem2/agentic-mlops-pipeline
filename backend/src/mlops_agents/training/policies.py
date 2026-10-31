"""Training policies store and proposal kind (Task 8, #493)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import desc, insert, select, update

from mlops_agents.agents.tools.base import Preview
from mlops_agents.clock import to_iso
from mlops_agents.ids import new_id
from mlops_agents.proposals.contract import (
    TRAINING_POLICY_KIND,
    ApplyResult,
    CredentialSlot,
    ProposalKind,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import training_policies
from mlops_agents.tracking.contract import Page
from mlops_agents.training.contract import (
    TrainingPolicyPayload,
    TrainingPolicyView,
)

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.monitoring.hooks.contract import Hooks, HookView
    from mlops_agents.monitoring.rules.contract import Rules
    from mlops_agents.providers.failover import Clock


def _payload(payload: BaseModel) -> TrainingPolicyPayload:
    if isinstance(payload, TrainingPolicyPayload):
        return payload
    return TrainingPolicyPayload.model_validate(payload.model_dump())


def _parse_iso(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SqliteTrainingPolicies:
    """SQLite-backed store for training policies."""

    def __init__(
        self,
        databases: Databases,
        clock: Clock,
        events: EventLog,
    ) -> None:
        self._databases = databases
        self._clock = clock
        self._events = events

    def _row_to_view(self, row: dict[str, Any]) -> TrainingPolicyView:
        payload_data = json.loads(str(row["payload"]))
        payload = TrainingPolicyPayload.model_validate(payload_data)
        return TrainingPolicyView(
            id=str(row["id"]),
            payload=payload,
            status=cast(Literal["active", "suspended", "disabled"], str(row["status"])),
            suspended_reason=(
                str(row["suspended_reason"]) if row["suspended_reason"] is not None else None
            ),
            proposal_id=str(row["proposal_id"]),
            approved=_parse_iso(str(row["approved"])),
        )

    async def list(self, project_id: str, model: str | None = None) -> list[TrainingPolicyView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(training_policies).order_by(desc(training_policies.c.approved))
            rows = (await conn.execute(stmt)).mappings().all()

        views = [self._row_to_view(dict(r)) for r in rows]
        if model is not None:
            views = [v for v in views if v.payload.model == model]
        return views

    async def get(self, project_id: str, policy_id: str) -> TrainingPolicyView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(training_policies).where(training_policies.c.id == policy_id)
            row = (await conn.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return self._row_to_view(dict(row))

    async def find_by_proposal(
        self, project_id: str, proposal_id: str
    ) -> TrainingPolicyView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(training_policies).where(training_policies.c.proposal_id == proposal_id)
            row = (await conn.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return self._row_to_view(dict(row))

    async def store(
        self, project_id: str, payload: TrainingPolicyPayload, proposal_id: str
    ) -> TrainingPolicyView:
        policy_id = new_id("pol")
        now_dt = self._clock.now()
        approved_iso = to_iso(now_dt)

        values = {
            "id": policy_id,
            "payload": payload.model_dump_json(),
            "status": "active",
            "suspended_reason": None,
            "proposal_id": proposal_id,
            "approved": approved_iso,
        }

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(insert(training_policies).values(**values))

        return TrainingPolicyView(
            id=policy_id,
            payload=payload,
            status="active",
            suspended_reason=None,
            proposal_id=proposal_id,
            approved=now_dt,
        )

    async def set_status(
        self,
        project_id: str,
        policy_id: str,
        status: Literal["active", "suspended", "disabled"],
        reason: str | None = None,
    ) -> TrainingPolicyView:
        engine = await self._databases.project(project_id)
        values: dict[str, Any] = {"status": status}
        if status == "suspended":
            values["suspended_reason"] = reason
        elif status == "active":
            values["suspended_reason"] = None
        elif reason is not None:
            values["suspended_reason"] = reason

        async with engine.begin() as conn:
            res = await conn.execute(
                update(training_policies)
                .where(training_policies.c.id == policy_id)
                .values(**values)
            )
            if res.rowcount == 0:
                raise KeyError(f"Policy {policy_id!r} not found in project {project_id!r}")

        updated = await self.get(project_id, policy_id)
        if updated is None:
            raise KeyError(f"Policy {policy_id!r} not found after update")
        return updated

    async def disable_active_for_model(self, project_id: str, model: str) -> None:
        policies = await self.list(project_id, model=model)
        active_policies = [p for p in policies if p.status == "active"]
        for p in active_policies:
            await self.set_status(project_id, p.id, "disabled", reason="superseded")

    async def suspend_for_hook(self, project_id: str, hook_id: str, reason: str) -> None:
        policies = await self.list(project_id)
        for pol in policies:
            if pol.status != "active":
                continue
            if pol.payload.train_hook_id == hook_id or pol.payload.evaluate_hook_id == hook_id:
                await self.set_status(project_id, pol.id, "suspended", reason=reason)
                await self._events.record(
                    project_id,
                    source="training",
                    type="policy.suspended",
                    subject_id=pol.id,
                    payload={
                        "policy_id": pol.id,
                        "hook_id": hook_id,
                        "reason": reason,
                    },
                )

    async def activate_for_hook(self, project_id: str, hook_id: str) -> None:
        policies = await self.list(project_id)
        for pol in policies:
            if pol.status != "suspended":
                continue
            if pol.payload.train_hook_id == hook_id or pol.payload.evaluate_hook_id == hook_id:
                await self.set_status(project_id, pol.id, "active", reason=None)

    async def page_policies(
        self,
        project_id: str,
        *,
        policy_id: str | None = None,
        model: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[TrainingPolicyView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(training_policies).order_by(desc(training_policies.c.approved))
            if policy_id is not None:
                stmt = stmt.where(training_policies.c.id == policy_id)
            if status is not None:
                stmt = stmt.where(training_policies.c.status == status)
            if since is not None:
                stmt = stmt.where(training_policies.c.approved >= to_iso(since))

            rows = (await conn.execute(stmt)).mappings().all()

        views = [self._row_to_view(dict(r)) for r in rows]
        if model is not None:
            views = [v for v in views if v.payload.model == model]

        total = len(views)
        page_rows = views[offset : offset + limit]
        truncated = offset + len(page_rows) < total
        return Page(rows=page_rows, total=total, truncated=truncated)


class TrainingPolicyKind(ProposalKind):
    """ProposalKind implementation for training policies."""

    name = TRAINING_POLICY_KIND
    payload_model = TrainingPolicyPayload

    def __init__(
        self,
        policies: SqliteTrainingPolicies,
        hooks: Hooks | None = None,
        rules: Rules | None = None,
        hook_lookup: Callable[[str], HookView | None] | None = None,
    ) -> None:
        self._policies = policies
        self._hooks = hooks
        self._rules = rules
        self._hook_cache: dict[str, HookView] = {}
        self._hook_lookup = hook_lookup

    def cache_hook(self, hook: HookView) -> None:
        self._hook_cache[hook.id] = hook

    def _get_hook(self, hook_id: str) -> HookView | None:
        if hook_id in self._hook_cache:
            return self._hook_cache[hook_id]
        if self._hook_lookup is not None:
            found = self._hook_lookup(hook_id)
            if found is not None:
                self._hook_cache[hook_id] = found
                return found
        return None

    def key(self, payload: BaseModel) -> str:
        return _payload(payload).model

    def preview(self, payload: BaseModel) -> Preview:
        p = _payload(payload)
        lines = [
            f"Model: {p.model}",
            f"Train hook: {p.train_hook_id}",
            f"Evaluate hook: {p.evaluate_hook_id or 'none'}",
            f"Window: kind={p.window.kind}, days={p.window.days}, start={p.window.start}, end={p.window.end}",
            f"Limits: min_interval_hours={p.min_interval_hours}, max_runs_per_week={p.max_runs_per_week}, run_time_limit_minutes={p.run_time_limit_minutes}",
        ]
        return Preview(kind="text", title=f"Training Policy: {p.model}", body="\n".join(lines))

    def warnings(self, payload: BaseModel) -> list[str]:
        p = _payload(payload)
        warns: list[str] = []

        train_hook = self._get_hook(p.train_hook_id)
        eval_hook = self._get_hook(p.evaluate_hook_id) if p.evaluate_hook_id else None

        deploys = (train_hook is not None and train_hook.spec.deploys) or (
            eval_hook is not None and eval_hook.spec.deploys
        )
        if deploys:
            warns.append("The cited hook deploys to production.")

        warns.append("The run-time limit is the team's to enforce.")

        is_command = (train_hook is not None and train_hook.spec.type == "command") or (
            eval_hook is not None and eval_hook.spec.type == "command"
        )
        if is_command:
            warns.append(
                "Command hook: training request (trq) rows are counted towards rate limits (no CI history)."
            )

        return warns

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        p = _payload(payload)

        # 1. Idempotent per proposal
        existing = await self._policies.find_by_proposal(project_id, proposal_id)
        if existing is not None:
            return ApplyResult(applied=[f"policy:{existing.id}"], failed={})

        # Fetch hooks to check if deploying
        train_hook = None
        if self._hooks is not None:
            train_hook = await self._hooks.get(project_id, p.train_hook_id)
            if train_hook is not None:
                self.cache_hook(train_hook)

        eval_hook = None
        if self._hooks is not None and p.evaluate_hook_id:
            eval_hook = await self._hooks.get(project_id, p.evaluate_hook_id)
            if eval_hook is not None:
                self.cache_hook(eval_hook)

        deploys = (train_hook is not None and train_hook.spec.deploys) or (
            eval_hook is not None and eval_hook.spec.deploys
        )

        # 2. Refuse deploying hook without enabled rollback rule
        if deploys:
            if self._rules is None:
                return ApplyResult(
                    applied=[],
                    failed={
                        "policy": f"Deploying hook requires an enabled rollback rule for model '{p.model}'"
                    },
                )
            rules = await self._rules.list(project_id, model=p.model)
            has_rollback = any(r.enabled and r.spec.hook_kind == "rollback" for r in rules)
            if not has_rollback:
                return ApplyResult(
                    applied=[],
                    failed={
                        "policy": f"Deploying hook requires an enabled rollback rule for model '{p.model}'"
                    },
                )

        # 3. Supersede previous active policy for this model
        await self._policies.disable_active_for_model(project_id, p.model)

        # 4. Store new active policy
        policy = await self._policies.store(project_id, p, proposal_id)
        return ApplyResult(applied=[f"policy:{policy.id}"], failed={})
