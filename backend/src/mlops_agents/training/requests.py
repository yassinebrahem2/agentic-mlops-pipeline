"""Training requests store (SqliteTraining) (Task 3, #488)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import ColumnElement, desc, func, insert, select, update

from mlops_agents.clock import to_iso
from mlops_agents.errors import DomainError
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import training_requests
from mlops_agents.tracking.contract import Page
from mlops_agents.training.contract import (
    Training,
    TrainingPolicyView,
    TrainingRequestView,
    TrainingWindow,
)
from mlops_agents.training.policies import SqliteTrainingPolicies

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.monitoring.hooks.contract import Hooks
    from mlops_agents.providers.failover import Clock


class SqliteTraining(Training):
    """SQLite-backed training requests store."""

    def __init__(
        self,
        databases: Databases,
        events: EventLog,
        clock: Clock,
        policies: SqliteTrainingPolicies | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self._databases = databases
        self._events = events
        self._clock = clock
        self._policies = policies or SqliteTrainingPolicies(databases, clock, events)
        self._hooks = hooks

    def bind_hooks(self, hooks: Hooks) -> None:
        self._hooks = hooks

    @property
    def policies_store(self) -> SqliteTrainingPolicies:
        return self._policies

    async def policies(self, project_id: str, model: str | None = None) -> list[TrainingPolicyView]:
        return await self._policies.list(project_id, model=model)

    async def get_policy(self, project_id: str, policy_id: str) -> TrainingPolicyView | None:
        return await self._policies.get(project_id, policy_id)

    async def set_policy_enabled(
        self, project_id: str, policy_id: str, enabled: bool
    ) -> TrainingPolicyView:
        if enabled:
            policy = await self._policies.get(project_id, policy_id)
            if policy is None:
                raise KeyError(policy_id)
            if self._hooks is not None:
                hook = await self._hooks.get(project_id, policy.payload.train_hook_id)
                if hook and hook.status == "suspended":
                    raise DomainError(
                        "hook_suspended",
                        f"Cannot enable policy '{policy_id}' while hook '{hook.id}' is suspended.",
                        status=409,
                    )
                if policy.payload.evaluate_hook_id:
                    eval_hook = await self._hooks.get(project_id, policy.payload.evaluate_hook_id)
                    if eval_hook and eval_hook.status == "suspended":
                        raise DomainError(
                            "hook_suspended",
                            f"Cannot enable policy '{policy_id}' while hook '{eval_hook.id}' is suspended.",
                            status=409,
                        )
        status = "active" if enabled else "disabled"
        return await self._policies.set_status(project_id, policy_id, status)

    async def suspend_policies_for_hook(self, project_id: str, hook_id: str, reason: str) -> None:
        await self._policies.suspend_for_hook(project_id, hook_id, reason)

    async def activate_policies_for_hook(self, project_id: str, hook_id: str) -> None:
        await self._policies.activate_for_hook(project_id, hook_id)

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
        return await self._policies.page_policies(
            project_id,
            policy_id=policy_id,
            model=model,
            status=status,
            since=since,
            limit=limit,
            offset=offset,
        )

    async def request(self, project_id: str, request_id: str) -> TrainingRequestView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(training_requests).where(training_requests.c.id == request_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return self._row_to_view(row)

    async def requests(
        self, project_id: str, model: str, limit: int = 50
    ) -> list[TrainingRequestView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(training_requests)
                        .where(training_requests.c.model == model)
                        .order_by(desc(training_requests.c.started))
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_view(row) for row in rows]

    async def create_request(
        self,
        project_id: str,
        request_id: str,
        model: str,
        hook_id: str,
        mode: Literal["train", "evaluate"],
        status: Literal["started", "linked", "failed", "silent"] = "started",
        fix_id: str | None = None,
        policy_id: str | None = None,
        window: TrainingWindow | None = None,
    ) -> TrainingRequestView:
        now_dt = self._clock.now()
        started_iso = to_iso(now_dt)

        window_json: str | None = None
        if window is not None:
            window_json = json.dumps(
                {
                    "start": to_iso(window.start),
                    "end": to_iso(window.end),
                }
            )

        values = {
            "id": request_id,
            "fix_id": fix_id,
            "model": model,
            "hook_id": hook_id,
            "policy_id": policy_id,
            "window": window_json,
            "mode": mode,
            "status": status,
            "run_key": None,
            "ci_run_url": None,
            "started": started_iso,
        }

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(insert(training_requests).values(**values))

        return TrainingRequestView(
            id=request_id,
            fix_id=fix_id,
            model=model,
            hook_id=hook_id,
            policy_id=policy_id,
            window=window,
            mode=mode,
            status=status,
            run_key=None,
            ci_run_url=None,
            started=now_dt,
        )

    async def set_status(
        self,
        project_id: str,
        request_id: str,
        status: Literal["started", "linked", "failed", "silent"],
        run_key: str | None = None,
        ci_run_url: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if run_key is not None:
            values["run_key"] = run_key
        if ci_run_url is not None:
            values["ci_run_url"] = ci_run_url

        stmt = update(training_requests).where(training_requests.c.id == request_id)
        if status == "failed":
            stmt = stmt.where(training_requests.c.status == "started")

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(stmt.values(**values))

    async def link(
        self,
        project_id: str,
        request_id: str,
        run_key: str,
        ci_run_url: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": "linked",
            "run_key": run_key,
        }
        if ci_run_url is not None:
            values["ci_run_url"] = ci_run_url

        stmt = (
            update(training_requests).where(training_requests.c.id == request_id).values(**values)
        )
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(stmt)

    async def mark_silent(self, project_id: str, request_id: str) -> None:
        stmt = (
            update(training_requests)
            .where(
                training_requests.c.id == request_id,
                training_requests.c.status == "started",
            )
            .values(status="silent")
        )
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(stmt)

    async def started_requests(self, project_id: str) -> list[TrainingRequestView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(training_requests)
                        .where(training_requests.c.status.in_(["started", "silent"]))
                        .order_by(training_requests.c.started.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_view(row) for row in rows]

    async def list_requests(
        self,
        project_id: str,
        model: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TrainingRequestView]:
        engine = await self._databases.project(project_id)
        stmt = select(training_requests)
        if model is not None:
            stmt = stmt.where(training_requests.c.model == model)
        if status is not None:
            stmt = stmt.where(training_requests.c.status == status)
        stmt = stmt.order_by(desc(training_requests.c.started)).limit(limit).offset(offset)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [self._row_to_view(row) for row in rows]

    async def page_requests(
        self,
        project_id: str,
        *,
        request_id: str | None = None,
        model: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[TrainingRequestView]:
        filters: list[ColumnElement[bool]] = []
        if request_id is not None:
            filters.append(training_requests.c.id == request_id)
        if model is not None:
            filters.append(training_requests.c.model == model)
        if status is not None:
            filters.append(training_requests.c.status == status)
        if since is not None:
            filters.append(training_requests.c.started >= to_iso(since))

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            count_stmt = select(func.count()).select_from(training_requests)
            if filters:
                count_stmt = count_stmt.where(*filters)
            total = int((await conn.scalar(count_stmt)) or 0)

            select_stmt = select(training_requests)
            if filters:
                select_stmt = select_stmt.where(*filters)
            select_stmt = (
                select_stmt.order_by(desc(training_requests.c.started)).limit(limit).offset(offset)
            )
            rows = (await conn.execute(select_stmt)).mappings().all()

        views = [self._row_to_view(r) for r in rows]
        return Page(rows=views, total=total, truncated=(offset + len(views)) < total)

    def _row_to_view(self, row: Any) -> TrainingRequestView:
        window_raw = row["window"]
        window: TrainingWindow | None = None
        if window_raw:
            data = json.loads(window_raw) if isinstance(window_raw, str) else window_raw
            start_dt = datetime.fromisoformat(data["start"])
            end_dt = datetime.fromisoformat(data["end"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            window = TrainingWindow(start=start_dt, end=end_dt)

        started_raw = row["started"]
        started_dt = (
            datetime.fromisoformat(started_raw) if isinstance(started_raw, str) else started_raw
        )
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=UTC)

        return TrainingRequestView(
            id=row["id"],
            fix_id=row["fix_id"],
            model=row["model"],
            hook_id=row["hook_id"],
            policy_id=row["policy_id"],
            window=window,
            mode=row["mode"],
            status=row["status"],
            run_key=row["run_key"],
            ci_run_url=row["ci_run_url"],
            started=started_dt,
        )
