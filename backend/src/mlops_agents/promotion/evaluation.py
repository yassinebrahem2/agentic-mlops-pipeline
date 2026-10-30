"""EvaluationReader and comparable implementations for Stage 5 promotion (I37)."""

from __future__ import annotations

import logging

from pydantic import ValidationError

from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.promotion.contract import (
    EvaluationPayload,
    EvaluationReader,
    EvaluationView,
)
from mlops_agents.tracking.contract import EvaluationRecord, Tracking, TrackingQuery

logger = logging.getLogger(__name__)

__all__ = ["SqliteEvaluationReader", "comparable"]


def comparable(a: EvaluationPayload, b: EvaluationPayload) -> bool:
    """Return True if both payloads share the same evaluation_commit and gate_config_version."""
    return (
        a.evaluation_commit == b.evaluation_commit
        and a.gate_config_version == b.gate_config_version
    )


class SqliteEvaluationReader(EvaluationReader):
    """Reads evaluation records via Tracking.query and builds EvaluationViews."""

    def __init__(self, tracking: Tracking, tasks: Tasks) -> None:
        self._tracking = tracking
        self._tasks = tasks
        self._warned_records: set[tuple[str, str, str, str]] = set()

    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None:
        page = await self._tracking.query(
            project_id,
            TrackingQuery(what="evaluations", model=model, version=version),
        )
        records = [
            r if isinstance(r, EvaluationRecord) else EvaluationRecord.model_validate(r)
            for r in page.rows
        ]
        if not records:
            return None

        # Multiple sources: newest fetched_at wins
        newest = max(records, key=lambda r: r.fetched_at)
        return await self._build_view(project_id, newest)

    async def latest(self, project_id: str, model: str) -> EvaluationView | None:
        page = await self._tracking.query(
            project_id,
            TrackingQuery(what="evaluations", model=model),
        )
        records = [
            r if isinstance(r, EvaluationRecord) else EvaluationRecord.model_validate(r)
            for r in page.rows
        ]
        if not records:
            return None

        # Multiple sources/versions: newest fetched_at wins
        newest = max(records, key=lambda r: r.fetched_at)
        return await self._build_view(project_id, newest)

    async def _build_view(self, project_id: str, record: EvaluationRecord) -> EvaluationView | None:
        if record.schema_version != 1:
            self._log_invalid_record(record)
            return None

        try:
            payload = EvaluationPayload.model_validate(record.payload)
        except ValidationError:
            self._log_invalid_record(record)
            return None

        if payload.schema_version != 1:
            self._log_invalid_record(record)
            return None

        open_tasks = await self._tasks.open_tasks(project_id)
        not_computed: dict[str, str | None] = {}

        for name, field_info in EvaluationPayload.model_fields.items():
            if not field_info.is_required() and getattr(payload, name) is None:
                matching_pr: str | None = None
                for task in open_tasks:
                    if task.agent == "deployment" and task.pr_number is not None:
                        title_lower = task.title.lower()
                        if (
                            name.lower() in title_lower
                            or name.replace("_", " ").lower() in title_lower
                        ):
                            matching_pr = str(task.pr_number)
                            break
                not_computed[name] = matching_pr

        return EvaluationView(
            record=record,
            payload=payload,
            not_computed=not_computed,
        )

    def _log_invalid_record(self, record: EvaluationRecord) -> None:
        key = (record.source_id, record.model, record.version, record.run_key)
        if key not in self._warned_records:
            self._warned_records.add(key)
            logger.warning(
                "Evaluation record for model %s version %s (source %s, run %s) failed payload validation or schema_version is not 1",
                record.model,
                record.version,
                record.source_id,
                record.run_key,
            )
