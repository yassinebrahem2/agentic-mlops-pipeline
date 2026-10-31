"""The `hooks` proposal kind: registers action hooks from proposals (I32)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from mlops_agents.agents.tools.base import Preview
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import Hooks, HooksPayload
from mlops_agents.proposals.contract import HOOKS_KIND, ApplyResult, CredentialSlot, hooks_key

logger = logging.getLogger(__name__)


def _payload(payload: BaseModel) -> HooksPayload:
    if not isinstance(payload, HooksPayload):
        raise TypeError(f"the hooks kind got a {type(payload).__name__}")
    return payload


class HooksKind:
    name = HOOKS_KIND
    payload_model: type[BaseModel] = HooksPayload

    def __init__(self, hooks: Hooks) -> None:
        self._hooks = hooks

    def key(self, payload: BaseModel) -> str:
        return hooks_key(_payload(payload))

    def preview(self, payload: BaseModel) -> Preview:
        hp = _payload(payload)
        lines: list[str] = []
        for h in hp.hooks:
            target = f"{h.name} (model: {h.model})" if h.model else h.name
            lines.append(f"{target} ({h.spec.kind}, {h.spec.type}): why: {h.why}")
        return Preview(kind="text", title="Hooks", body="\n".join(lines))

    def warnings(self, payload: BaseModel) -> list[str]:
        hp = _payload(payload)
        out: list[str] = []
        for h in hp.hooks:
            if h.spec.deploys:
                target = f"'{h.name}' (model: {h.model})" if h.model else f"'{h.name}'"
                out.append(f"Hook {target} deploys.")
        return out

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        hp = _payload(payload)
        applied: list[str] = []
        failed: dict[str, str] = {}
        for h in hp.hooks:
            part = f"hook:{h.model}:{h.name}" if h.model else f"hook:{h.name}"
            try:
                await self._hooks.register(
                    project_id,
                    spec=h.spec,
                    name=h.name,
                    model=h.model,
                    source="proposal",
                )
                applied.append(part)
            except DomainError as exc:
                failed[part] = exc.message
            except Exception as exc:
                logger.exception("Registering hook part %s failed", part)
                failed[part] = str(exc)
        return ApplyResult(applied=applied, failed=failed)
