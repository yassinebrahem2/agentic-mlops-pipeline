"""Training requests store (SqliteTraining) (Task 3, #488)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import desc, insert, select, update

from mlops_agents.clock import to_iso
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import training_requests
from mlops_agents.training.contract import (
    Training,
    TrainingPolicyView,
    TrainingRequestView,
    TrainingWindow,
)

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.providers.failover import Clock


class SqliteTraining(Training):
    """SQLite-backed training requests store."""

    def __init__(
        self,
        databases: Databases,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._events = events
        self._clock = clock

    async def policies(self, project_id: str, model: str | None = None) -> list[TrainingPolicyView]:
        # Policies store is introduced in Task 8
        return []

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
