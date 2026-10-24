"""SQLite-backed AuditLog implementation (I11)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any, cast

from sqlalchemy import insert

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.clock import now_iso
from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.events.contract import AuditDecision, AuditLog, AuditRecord
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit


def _scrub_string(val: str, secrets: Iterable[str]) -> str:
    s = scrub_url_secrets(val)
    s = scrub_values(s, secrets)
    if len(s) > 4000:
        s = s[:4000]
    return s


def _scrub_and_cap(item: object, secrets: Iterable[str]) -> object:
    if isinstance(item, str):
        return _scrub_string(item, secrets)
    if isinstance(item, dict):
        mapping = cast(dict[object, object], item)
        return {str(k): _scrub_and_cap(v, secrets) for k, v in mapping.items()}
    if isinstance(item, list):
        sequence = cast(list[object], item)
        return [_scrub_and_cap(v, secrets) for v in sequence]
    return item


class SqliteAuditLog(AuditLog):
    """Audit log backed by project.db with credential scrubbing and value length limits."""

    def __init__(
        self,
        databases: Databases,
        *,
        secrets: Iterable[str] | Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._databases = databases
        self._secrets = secrets

    def _get_secrets(self) -> Iterable[str]:
        if self._secrets is None:
            return []
        if callable(self._secrets):
            return self._secrets()
        return self._secrets

    async def record(
        self,
        project_id: str,
        *,
        actor: str,
        action: str,
        risk_class: RiskClass,
        permission_level: PermissionLevel,
        decision: AuditDecision,
        target: str,
        detail: dict[str, Any],
    ) -> AuditRecord:
        aud_id = new_id("aud")
        ts = now_iso()
        known_secrets = self._get_secrets()
        scrubbed_detail: dict[str, Any] = {
            k: _scrub_and_cap(v, known_secrets) for k, v in detail.items()
        }
        detail_json = json.dumps(scrubbed_detail)

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                insert(audit).values(
                    id=aud_id,
                    ts=ts,
                    actor=actor,
                    action=action,
                    risk_class=risk_class,
                    permission_level=permission_level,
                    decision=decision,
                    target=target,
                    detail=detail_json,
                )
            )

        return AuditRecord(
            id=aud_id,
            ts=ts,
            actor=actor,
            action=action,
            risk_class=risk_class,
            permission_level=permission_level,
            decision=decision,
            target=target,
            detail=scrubbed_detail,
        )
