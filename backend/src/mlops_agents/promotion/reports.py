"""SqliteReports implementation (Task 2, #496)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS
from mlops_agents.promotion.contract import (
    EvaluationReader,
    ReportDraft,
    ReportField,
    Reports,
    ReportView,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import reports

__all__ = ["SqliteReports"]


class SqliteReports(Reports):
    """Sqlite-backed Reports service storing reports in project.db."""

    def __init__(
        self,
        databases: Databases,
        reader: EvaluationReader,
        events: EventLog,
    ) -> None:
        self._databases = databases
        self._reader = reader
        self._events = events

    async def save(
        self,
        project_id: str,
        draft: ReportDraft,
        author: str,
    ) -> ReportView:
        missing = [k for k in REQUIRED_REPORT_FIELDS if k not in draft.assessments]
        if missing:
            raise DomainError(
                "missing_required_report_fields",
                f"Missing required report fields: {', '.join(sorted(missing))}",
                status=422,
            )

        report_id = new_id("rep")
        now_str = now_iso()
        created_dt = datetime.fromisoformat(now_str)
        assessments_json = json.dumps(
            {k: v.model_dump(mode="json") for k, v in draft.assessments.items()}
        )

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                reports.insert().values(
                    id=report_id,
                    model=draft.model,
                    candidate_version=draft.candidate_version,
                    fix_id=draft.fix_id,
                    training_request_id=draft.training_request_id,
                    assessments=assessments_json,
                    author=author,
                    created=now_str,
                )
            )

        await self._events.record(
            project_id,
            source=author,
            type="report.saved",
            payload={
                "report_id": report_id,
                "model": draft.model,
                "candidate_version": draft.candidate_version,
            },
            subject_id=report_id,
        )

        evaluation = await self._reader.for_candidate(
            project_id, draft.model, draft.candidate_version
        )

        return ReportView(
            id=report_id,
            draft=draft,
            evaluation=evaluation,
            created=created_dt,
            author=author,
        )

    async def get(self, project_id: str, report_id: str) -> ReportView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            result = await conn.execute(select(reports).where(reports.c.id == report_id))
            row = result.mappings().first()

        if row is None:
            return None

        return await self._row_to_view(project_id, row)

    async def for_candidate(
        self,
        project_id: str,
        model: str,
        version: str,
    ) -> ReportView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            result = await conn.execute(
                select(reports)
                .where(
                    reports.c.model == model,
                    reports.c.candidate_version == version,
                )
                .order_by(reports.c.created.desc(), reports.c.id.desc())
                .limit(1)
            )
            row = result.mappings().first()

        if row is None:
            return None

        return await self._row_to_view(project_id, row)

    async def _row_to_view(self, project_id: str, row: Any) -> ReportView:
        raw_assessments = json.loads(str(row["assessments"]))
        assessments = {k: ReportField.model_validate(v) for k, v in raw_assessments.items()}
        draft = ReportDraft(
            model=str(row["model"]),
            candidate_version=str(row["candidate_version"]),
            fix_id=row["fix_id"],
            training_request_id=row["training_request_id"],
            assessments=assessments,
        )
        evaluation = await self._reader.for_candidate(
            project_id, str(row["model"]), str(row["candidate_version"])
        )
        return ReportView(
            id=str(row["id"]),
            draft=draft,
            evaluation=evaluation,
            created=datetime.fromisoformat(str(row["created"])),
            author=str(row["author"]),
        )
