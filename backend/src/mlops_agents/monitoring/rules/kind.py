"""The `rule` proposal kind: registers monitoring rules from proposals (I32)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import Hooks
from mlops_agents.monitoring.rules.contract import RulePayload, Rules
from mlops_agents.proposals.contract import (
    RULE_KIND,
    ApplyResult,
    CredentialSlot,
    rule_key,
)

logger = logging.getLogger(__name__)


def _payload(payload: BaseModel) -> RulePayload:
    if not isinstance(payload, RulePayload):
        raise TypeError(f"the rule kind got a {type(payload).__name__}")
    return payload


class RuleKind:
    """The proposal kind for monitoring rules."""

    name = RULE_KIND
    payload_model: type[BaseModel] = RulePayload

    def __init__(
        self,
        rules: Rules,
        hooks: Hooks,
        hook_deploys: Callable[[str], bool] | None = None,
    ) -> None:
        self._rules = rules
        self._hooks = hooks
        self._hook_deploys = hook_deploys
        self._known_deploys: dict[str, bool] = {}

    def set_hook_deploys(self, hook_id: str, deploys: bool) -> None:
        """Cache whether a hook deploys for warnings."""
        self._known_deploys[hook_id] = deploys

    def key(self, payload: BaseModel) -> str:
        return rule_key(_payload(payload))

    def preview(self, payload: BaseModel) -> Preview:
        rp = _payload(payload)
        spec = rp.spec
        lines: list[str] = [
            f"Rule: {spec.name}",
            f"Model: {spec.model or 'project-wide'}",
            f"Why: {spec.why}",
            f"Hook: {spec.hook_id} ({spec.hook_kind})",
            f"Limits: {spec.limits.per_incident} per incident, {spec.limits.per_day} per day",
            "Terms:",
        ]
        for term in spec.terms:
            m_str = f" [model: {term.model}]" if term.model else ""
            lines.append(
                f"  - Check '{term.check_id}'{m_str}: {term.quantifier} in {term.window_minutes}m (min {term.min_records}) when {term.when}"
            )
        return Preview(kind="text", title=f"Rule: {spec.name}", body="\n".join(lines))

    def warnings(self, payload: BaseModel) -> list[str]:
        rp = _payload(payload)
        hook_id = rp.spec.hook_id
        deploys = False

        if self._hook_deploys is not None:
            deploys = self._hook_deploys(hook_id)
        elif hook_id in self._known_deploys:
            deploys = self._known_deploys[hook_id]
        else:
            checker: Any = getattr(self._hooks, "hook_deploys", None)
            if callable(checker):
                deploys = bool(checker(hook_id))
            else:
                is_deploys: Any = getattr(self._hooks, "is_deploys", None)
                if callable(is_deploys):
                    deploys = bool(is_deploys(hook_id))

        if deploys:
            return [f"Rule hook '{hook_id}' deploys."]
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        rp = _payload(payload)
        spec = rp.spec
        part = f"rule:{spec.model}:{spec.name}" if spec.model else f"rule:{spec.name}"

        hook = await self._hooks.get(project_id, spec.hook_id)
        if hook is None:
            return ApplyResult(applied=[], failed={part: f"Hook '{spec.hook_id}' not found."})

        self._known_deploys[spec.hook_id] = hook.spec.deploys

        if hook.spec.kind != spec.hook_kind:
            return ApplyResult(
                applied=[],
                failed={
                    part: (
                        f"Hook '{spec.hook_id}' kind '{hook.spec.kind}' does not match "
                        f"rule hook kind '{spec.hook_kind}'."
                    )
                },
            )

        if hook.model != spec.model:
            return ApplyResult(
                applied=[],
                failed={
                    part: (
                        f"Hook '{spec.hook_id}' model '{hook.model}' does not match "
                        f"rule model '{spec.model}'."
                    )
                },
            )

        try:
            await self._rules.store(project_id, spec=spec, proposal_id=proposal_id)
            return ApplyResult(applied=[part], failed={})
        except DomainError as exc:
            return ApplyResult(applied=[], failed={part: exc.message})
        except Exception as exc:
            logger.exception("Storing rule %s failed", part)
            return ApplyResult(applied=[], failed={part: str(exc)})
