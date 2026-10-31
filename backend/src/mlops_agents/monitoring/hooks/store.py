"""Database store for hooks, hook_pins, and hook_runs tables."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.hooks.contract import (
    HookRunView,
    HookView,
    PinnedFile,
)
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import hook_pins, hook_runs, hooks


def parse_iso(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def row_to_hook_run_view(row: Mapping[Any, Any]) -> HookRunView:
    evidence_val = row["evidence"]
    evidence_list: list[str] = []
    if isinstance(evidence_val, str):
        try:
            parsed = cast(object, json.loads(evidence_val))
            if isinstance(parsed, list):
                parsed_list = cast(list[object], parsed)
                evidence_list = [str(item) for item in parsed_list]
        except Exception:
            evidence_list = []
    elif isinstance(evidence_val, list):
        val_list = cast(list[object], evidence_val)
        evidence_list = [str(item) for item in val_list]

    ok_val = None
    if row["ok"] is not None:
        ok_val = bool(row["ok"])

    return HookRunView(
        id=str(row["id"]),
        hook_id=str(row["hook_id"]),
        kind=str(row["kind"]),
        model=str(row["model"]) if row["model"] is not None else None,
        incident_id=str(row["incident_id"]) if row["incident_id"] is not None else None,
        fix_id=str(row["fix_id"]) if row["fix_id"] is not None else None,
        rule_id=str(row["rule_id"]) if row["rule_id"] is not None else None,
        policy_id=str(row["policy_id"]) if row["policy_id"] is not None else None,
        reason=str(row["reason"]),
        basis=row["basis"],
        basis_ref=str(row["basis_ref"]) if row["basis_ref"] is not None else None,
        evidence=evidence_list,
        ok=ok_val,
        detail=str(row["detail"]) if row["detail"] is not None else None,
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        started=parse_iso(str(row["started"])),
        ended=parse_iso(str(row["ended"])) if row["ended"] is not None else None,
    )


def make_hook_view(
    row: Mapping[Any, Any],
    pins: list[PinnedFile],
    credential_set: bool,
) -> HookView:
    deploys_verified = None
    if row["deploys_verified"] is not None:
        deploys_verified = bool(row["deploys_verified"])

    raw_spec = row["spec"]
    spec_dict = json.loads(raw_spec) if isinstance(raw_spec, str) else raw_spec
    spec = CommandSpec.model_validate(spec_dict)

    return HookView(
        id=str(row["id"]),
        name=str(row["name"]),
        model=str(row["model"]) if row["model"] is not None else None,
        spec=spec,
        status=row["status"],
        suspended_reason=(
            str(row["suspended_reason"]) if row["suspended_reason"] is not None else None
        ),
        credential_set=credential_set,
        pins=pins,
        approved_commit=(
            str(row["approved_commit"]) if row["approved_commit"] is not None else None
        ),
        deploys_verified=deploys_verified,
        source=row["source"],
        created=parse_iso(str(row["created"])),
    )


class HooksStore:
    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def get(
        self, project_id: str, hook_id: str
    ) -> tuple[dict[str, Any], list[PinnedFile]] | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(hooks).where(hooks.c.id == hook_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            pin_rows = (
                (
                    await conn.execute(
                        select(hook_pins)
                        .where(hook_pins.c.hook == hook_id)
                        .order_by(hook_pins.c.path.asc())
                    )
                )
                .mappings()
                .all()
            )
            pins = [PinnedFile(path=str(p["path"]), sha256=str(p["sha256"])) for p in pin_rows]
            return dict(row), pins

    async def get_by_model_name(
        self, project_id: str, model: str | None, name: str
    ) -> tuple[dict[str, Any], list[PinnedFile]] | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(hooks).where(
                            func.coalesce(hooks.c.model, "") == (model or ""),
                            hooks.c.name == name,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            hook_id = str(row["id"])
            pin_rows = (
                (
                    await conn.execute(
                        select(hook_pins)
                        .where(hook_pins.c.hook == hook_id)
                        .order_by(hook_pins.c.path.asc())
                    )
                )
                .mappings()
                .all()
            )
            pins = [PinnedFile(path=str(p["path"]), sha256=str(p["sha256"])) for p in pin_rows]
            return dict(row), pins

    async def list(
        self, project_id: str, model: str | None = None
    ) -> list[tuple[dict[str, Any], list[PinnedFile]]]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            query = select(hooks).order_by(hooks.c.created.asc(), hooks.c.id.asc())
            if model is not None:
                query = query.where(hooks.c.model == model)
            hook_rows = (await conn.execute(query)).mappings().all()
            if not hook_rows:
                return []
            hook_ids = [str(h["id"]) for h in hook_rows]
            pins_query = (
                select(hook_pins)
                .where(hook_pins.c.hook.in_(hook_ids))
                .order_by(hook_pins.c.path.asc())
            )
            pin_rows = (await conn.execute(pins_query)).mappings().all()
            pins_by_hook: dict[str, list[PinnedFile]] = {h_id: [] for h_id in hook_ids}
            for p in pin_rows:
                pins_by_hook[str(p["hook"])].append(
                    PinnedFile(path=str(p["path"]), sha256=str(p["sha256"]))
                )
            return [(dict(h), pins_by_hook[str(h["id"])]) for h in hook_rows]

    async def insert_hook(
        self,
        project_id: str,
        row: dict[str, Any],
        pins: list[PinnedFile],
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(sqlite_insert(hooks).values(**row))
            if pins and row.get("approved_commit"):
                await conn.execute(
                    sqlite_insert(hook_pins),
                    [
                        {
                            "hook": row["id"],
                            "path": p.path,
                            "sha256": p.sha256,
                            "commit": row["approved_commit"],
                        }
                        for p in pins
                    ],
                )

    async def update_hook_spec(
        self,
        project_id: str,
        hook_id: str,
        spec: str,
        approved_commit: str | None = None,
        pins: list[PinnedFile] | None = None,
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            values: dict[str, Any] = {"spec": spec}
            if approved_commit is not None:
                values["approved_commit"] = approved_commit
            await conn.execute(update(hooks).where(hooks.c.id == hook_id).values(**values))
            if pins is not None and approved_commit is not None:
                await conn.execute(delete(hook_pins).where(hook_pins.c.hook == hook_id))
                if pins:
                    await conn.execute(
                        sqlite_insert(hook_pins),
                        [
                            {
                                "hook": hook_id,
                                "path": p.path,
                                "sha256": p.sha256,
                                "commit": approved_commit,
                            }
                            for p in pins
                        ],
                    )

    async def update_hook_status(
        self,
        project_id: str,
        hook_id: str,
        status: str,
        suspended_reason: str | None = None,
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(hooks)
                .where(hooks.c.id == hook_id)
                .values(status=status, suspended_reason=suspended_reason)
            )

    async def update_deploys_verified(self, project_id: str, hook_id: str, verified: bool) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(hooks)
                .where(hooks.c.id == hook_id)
                .values(deploys_verified=1 if verified else 0)
            )

    async def approve_hook(
        self,
        project_id: str,
        hook_id: str,
        approved_commit: str | None,
        pins: list[PinnedFile] | None,
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            values: dict[str, Any] = {
                "status": "active",
                "suspended_reason": None,
            }
            if approved_commit is not None:
                values["approved_commit"] = approved_commit
            await conn.execute(update(hooks).where(hooks.c.id == hook_id).values(**values))
            if pins is not None and approved_commit is not None:
                await conn.execute(delete(hook_pins).where(hook_pins.c.hook == hook_id))
                if pins:
                    await conn.execute(
                        sqlite_insert(hook_pins),
                        [
                            {
                                "hook": hook_id,
                                "path": p.path,
                                "sha256": p.sha256,
                                "commit": approved_commit,
                            }
                            for p in pins
                        ],
                    )

    async def insert_run(self, project_id: str, row: dict[str, Any]) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(sqlite_insert(hook_runs).values(**row))

    async def update_run_finish(
        self,
        project_id: str,
        run_id: str,
        ok: bool,
        detail: str,
        request_id: str | None,
        ended: str,
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            values: dict[str, Any] = {
                "ok": 1 if ok else 0,
                "detail": detail,
                "ended": ended,
            }
            if request_id is not None:
                values["request_id"] = request_id
            await conn.execute(update(hook_runs).where(hook_runs.c.id == run_id).values(**values))

    async def list_runs(
        self,
        project_id: str,
        *,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[HookRunView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            query = select(hook_runs).order_by(hook_runs.c.started.desc())
            if kinds is not None:
                query = query.where(hook_runs.c.kind.in_(kinds))
            if since is not None:
                query = query.where(hook_runs.c.started >= to_iso(since))
            query = query.limit(limit)
            rows = (await conn.execute(query)).mappings().all()
            return [row_to_hook_run_view(r) for r in rows]
