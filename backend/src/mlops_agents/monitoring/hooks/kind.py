"""The `hooks` proposal kind: registers action hooks from proposals (I32)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.agents.tools.base import Preview
from mlops_agents.errors import DomainError
from mlops_agents.monitoring.hooks.contract import Hooks, HooksPayload
from mlops_agents.monitoring.hooks.pins import hook_paths
from mlops_agents.proposals.contract import (
    HOOKS_KIND,
    ApplyResult,
    CredentialSlot,
    ProposalCheck,
    hooks_key,
)
from mlops_agents.storage.tables import file_claims, tasks

if TYPE_CHECKING:
    from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


def _payload(payload: BaseModel | dict[str, Any]) -> HooksPayload:
    if isinstance(payload, HooksPayload):
        return payload
    if isinstance(payload, dict):
        return HooksPayload.model_validate(payload)
    raise TypeError(f"the hooks kind got a {type(payload).__name__}")


class HooksKind(ProposalCheck):
    name = HOOKS_KIND
    payload_model: type[BaseModel] = HooksPayload

    def __init__(self, hooks: Hooks, databases: Databases | None = None) -> None:
        self._hooks = hooks
        self._databases = databases

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

    async def check(self, project_id: str, payload: Any) -> list[str]:
        if self._databases is None:
            return []
        hp = _payload(payload)
        paths: list[str] = []
        for h in hp.hooks:
            for p in hook_paths(h.spec):
                if p not in paths:
                    paths.append(p)
        if not paths:
            return []

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(file_claims.c.path, tasks.c.id, tasks.c.pr_number)
                .join(tasks, tasks.c.id == file_claims.c.task_id)
                .where(
                    tasks.c.status.in_(("active", "in_review")),
                    file_claims.c.path.in_(paths),
                )
            )
            rows = (await conn.execute(stmt)).all()

        path_order = {p: i for i, p in enumerate(paths)}
        sorted_rows = sorted(rows, key=lambda r: (path_order.get(r[0], 0), r[1]))

        refusals: list[str] = []
        seen: set[str] = set()
        for path, task_id, pr_number in sorted_rows:
            pr_str = f"PR #{pr_number}" if pr_number is not None else "no PR"
            msg = f"{path} is being changed in task {task_id} ({pr_str}); propose this hook after that PR merges"
            if msg not in seen:
                seen.add(msg)
                refusals.append(msg)
        return refusals

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
