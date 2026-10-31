"""RuleAuthority: evaluates rule citations and limits for action calls (I23, I30)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.agents.approvals.authority import ActionAuthority, ActionCall, Authority
from mlops_agents.monitoring.rules.contract import RuleView
from mlops_agents.monitoring.rules.evaluator import evaluate_rule, rule_limits_allow
from mlops_agents.monitoring.rules.store import SqliteRules
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases

if TYPE_CHECKING:
    from mlops_agents.monitoring.results.contract import Results
    from mlops_agents.monitoring.rules.contract import Rules


class RuleAuthority(ActionAuthority):
    """Authority for rule citations on rollback, rerun and fall_back actions.

    The hard limit: an agent cites a rule, it never certifies one.
    Nothing from the agent (its reason or claims) is read or trusted.
    """

    def __init__(
        self,
        rules: Rules,
        results: Results,
        databases: Databases,
        clock: Clock,
    ) -> None:
        self._rules = rules
        self._results = results
        self._databases = databases
        self._clock = clock

    async def authorize(self, call: ActionCall) -> Authority:
        ref = call.rule_id
        if not ref:
            return Authority(basis="none", ref=ref, reason="No rule cited", evidence=[])

        rule: RuleView | None
        if isinstance(self._rules, SqliteRules):
            rule = await self._rules.get(call.project_id, ref)
        else:
            all_rules = await self._rules.list(call.project_id)
            rule = next((r for r in all_rules if r.id == ref), None)
        if rule is None:
            return Authority(basis="none", ref=ref, reason=f"Rule {ref!r} not found", evidence=[])

        if not rule.enabled:
            return Authority(basis="none", ref=ref, reason=f"Rule {ref!r} is disabled", evidence=[])

        if rule.spec.hook_id != call.hook_id:
            return Authority(
                basis="none",
                ref=ref,
                reason=f"Rule hook_id {rule.spec.hook_id!r} does not match call hook_id {call.hook_id!r}",
                evidence=[],
            )

        if rule.spec.hook_kind != call.kind:
            return Authority(
                basis="none",
                ref=ref,
                reason=f"Rule hook_kind {rule.spec.hook_kind!r} does not match call kind {call.kind!r}",
                evidence=[],
            )

        if rule.spec.model != call.model:
            return Authority(
                basis="none",
                ref=ref,
                reason=f"Rule model {rule.spec.model!r} does not match call model {call.model!r}",
                evidence=[],
            )

        if call.incident_id is None:
            return Authority(
                basis="none",
                ref=ref,
                reason="Rule citation requires an incident_id",
                evidence=[],
            )

        now = self._clock.now()

        passed, evidence = await evaluate_rule(self._results, call.project_id, rule, now)
        if not passed:
            return Authority(
                basis="none",
                ref=ref,
                reason=f"Rule {ref!r} conditions are not satisfied",
                evidence=[],
            )

        allowed, reason = await rule_limits_allow(
            self._databases, call.project_id, rule, call.incident_id, now
        )
        if not allowed:
            return Authority(
                basis="none",
                ref=ref,
                reason=reason or f"Rule {ref!r} limits reached",
                evidence=[],
            )

        return Authority(
            basis="rule",
            ref=rule.id,
            reason=f"Rule {rule.id} satisfied",
            evidence=evidence,
        )
