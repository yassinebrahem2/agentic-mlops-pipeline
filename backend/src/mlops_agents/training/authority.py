"""ActionAuthority implementation for training policies (Task 8, #493)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mlops_agents.agents.approvals.authority import (
    ActionAuthority,
    ActionCall,
    Authority,
)
from mlops_agents.clock import to_iso
from mlops_agents.tracking.contract import CiRunRow, TrackingQuery

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks
    from mlops_agents.monitoring.rules.contract import Rules
    from mlops_agents.providers.failover import Clock
    from mlops_agents.tracking.contract import Tracking
    from mlops_agents.training.policies import SqliteTrainingPolicies
    from mlops_agents.training.requests import SqliteTraining


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class PolicyAuthority(ActionAuthority):
    """Authority checking training policy rate limits and requirements."""

    def __init__(
        self,
        policies: SqliteTrainingPolicies,
        training: SqliteTraining,
        tracking: Tracking,
        hooks: Hooks,
        clock: Clock,
        rules: Rules | None = None,
    ) -> None:
        self._policies = policies
        self._training = training
        self._tracking = tracking
        self._hooks = hooks
        self._clock = clock
        self._rules = rules

    async def authorize(self, call: ActionCall) -> Authority:
        if call.policy_id is None:
            return Authority(
                basis="none",
                ref=None,
                reason="No policy cited",
                evidence=[],
            )

        policy = await self._policies.get(call.project_id, call.policy_id)
        if policy is None:
            return Authority(
                basis="none",
                ref=call.policy_id,
                reason=f"Policy '{call.policy_id}' not found",
                evidence=[],
            )

        if policy.status != "active":
            return Authority(
                basis="none",
                ref=call.policy_id,
                reason=f"Policy '{call.policy_id}' is not active ({policy.status})",
                evidence=[],
            )

        if call.model is not None and policy.payload.model != call.model:
            return Authority(
                basis="none",
                ref=call.policy_id,
                reason=f"Model mismatch: policy is for '{policy.payload.model}', call is for '{call.model}'",
                evidence=[],
            )

        if call.hook_id is not None:
            allowed_hooks = {policy.payload.train_hook_id}
            if policy.payload.evaluate_hook_id:
                allowed_hooks.add(policy.payload.evaluate_hook_id)
            if call.hook_id not in allowed_hooks:
                return Authority(
                    basis="none",
                    ref=call.policy_id,
                    reason=f"Hook mismatch: hook '{call.hook_id}' not in policy",
                    evidence=[],
                )

        hook_id = call.hook_id or policy.payload.train_hook_id
        hook = await self._hooks.get(call.project_id, hook_id)
        if hook is None:
            return Authority(
                basis="none",
                ref=call.policy_id,
                reason=f"Hook '{hook_id}' not found",
                evidence=[],
            )

        now = _to_utc(self._clock.now())
        run_times: list[datetime] = []

        if hook.spec.type == "ci_dispatch":
            page = await self._tracking.query(
                call.project_id,
                TrackingQuery(what="ci_runs", limit=500),
            )
            matching_runs = [
                r for r in page.rows if isinstance(r, CiRunRow) and r.workflow == hook.spec.workflow
            ]
            run_times = [_to_utc(r.start) for r in matching_runs]
        else:
            reqs = await self._training.list_requests(
                call.project_id,
                model=policy.payload.model,
                limit=500,
            )
            matching_reqs = [r for r in reqs if r.hook_id == hook_id]
            run_times = [_to_utc(r.started) for r in matching_reqs]

        # 1. Check min_interval_hours
        if run_times:
            latest_run = max(run_times)
            if now - latest_run < timedelta(hours=policy.payload.min_interval_hours):
                return Authority(
                    basis="none",
                    ref=call.policy_id,
                    reason=(
                        f"min_interval_hours violated: last run was at {to_iso(latest_run)}, "
                        f"minimum interval is {policy.payload.min_interval_hours}h"
                    ),
                    evidence=[],
                )

        # 2. Check max_runs_per_week
        cutoff_7d = now - timedelta(days=7)
        runs_in_7d = [t for t in run_times if t >= cutoff_7d]
        if len(runs_in_7d) >= policy.payload.max_runs_per_week:
            return Authority(
                basis="none",
                ref=call.policy_id,
                reason=(
                    f"max_runs_per_week exceeded: {len(runs_in_7d)} runs in last 7 days "
                    f"(cap is {policy.payload.max_runs_per_week})"
                ),
                evidence=[],
            )

        # 3. Rollback rule requirement for deploying hook
        if hook.spec.deploys:
            if self._rules is None:
                return Authority(
                    basis="none",
                    ref=call.policy_id,
                    reason="Deploying hook requires an enabled rollback rule",
                    evidence=[],
                )
            rules = await self._rules.list(call.project_id, model=policy.payload.model)
            has_rollback = any(r.enabled and r.spec.hook_kind == "rollback" for r in rules)
            if not has_rollback:
                return Authority(
                    basis="none",
                    ref=call.policy_id,
                    reason="Deploying hook requires an enabled rollback rule",
                    evidence=[],
                )

        return Authority(
            basis="policy",
            ref=policy.id,
            reason="Policy limits satisfied",
            evidence=[],
        )
