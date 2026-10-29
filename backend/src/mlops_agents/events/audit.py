"""SQLite-backed AuditLog implementation (I11)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast

from sqlalchemy import and_, insert, or_, select

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.clock import now_iso
from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.events.contract import AuditDecision, AuditLog, AuditRecord
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit

SecretSource = Iterable[str] | Callable[[], Iterable[str]] | Callable[[], Awaitable[Iterable[str]]]


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
        out: dict[str, object] = {}
        for k, v in mapping.items():
            base_k = _scrub_string(str(k), secrets)
            target_k = base_k
            count = 1
            while target_k in out:
                suffix = f"_{count}"
                prefix = base_k[: 4000 - len(suffix)]
                target_k = f"{prefix}{suffix}"
                count += 1
            out[target_k] = _scrub_and_cap(v, secrets)
        return out
    if isinstance(item, tuple):
        elements = cast(tuple[object, ...], item)
        return tuple(_scrub_and_cap(v, secrets) for v in elements)
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
        secrets: SecretSource,
    ) -> None:
        self._databases = databases
        self._secrets = secrets

    async def _get_secrets(self) -> Iterable[str]:
        if callable(self._secrets):
            res = self._secrets()
            if inspect.isawaitable(res):
                return await res
            return res
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
        known_secrets = await self._get_secrets()
        scrubbed_detail = cast(dict[str, Any], _scrub_and_cap(detail, known_secrets))
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

    async def page(
        self,
        project_id: str,
        *,
        actor: str | None = None,
        before: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[AuditRecord]:
        """Newest first, keyset on (ts, id)."""
        engine = await self._databases.project(project_id)
        stmt = select(audit)
        if actor is not None:
            stmt = stmt.where(audit.c.actor == actor)
        if before is not None:
            before_ts, before_id = before
            stmt = stmt.where(
                or_(
                    audit.c.ts < before_ts,
                    and_(audit.c.ts == before_ts, audit.c.id < before_id),
                )
            )
        stmt = stmt.order_by(audit.c.ts.desc(), audit.c.id.desc()).limit(limit)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            AuditRecord(
                id=str(r.id),
                ts=str(r.ts),
                actor=str(r.actor),
                action=str(r.action),
                risk_class=r.risk_class,
                permission_level=r.permission_level,
                decision=r.decision,
                target=str(r.target),
                detail=json.loads(str(r.detail)),
            )
            for r in rows
        ]
