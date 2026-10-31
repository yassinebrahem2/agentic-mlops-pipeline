"""EvaluationReader and comparable implementations for Stage 5 promotion (I37)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

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


def _match_fields_in_title(title: str, field_names: Sequence[str]) -> set[str]:
    """Find all field names in a task title using exact token boundaries, prioritizing longer matches."""
    matches: list[tuple[int, int, str]] = []  # (start, end, field_name)
    for name in field_names:
        variants = {
            name.lower(),
            name.replace("_", " ").lower(),
            name.replace("_", "-").lower(),
        }
        for variant in variants:
            pattern = re.compile(
                r"(?<![a-zA-Z0-9_])" + re.escape(variant) + r"(?![a-zA-Z0-9_])",
                re.IGNORECASE,
            )
            for m in pattern.finditer(title):
                matches.append((m.start(), m.end(), name))

    if not matches:
        return set()

    # Sort matches by length descending so longer matches claim their character spans first
    matches.sort(key=lambda m: m[1] - m[0], reverse=True)

    claimed_spans: list[tuple[int, int]] = []
    matched_fields: set[str] = set()

    for start, end, name in matches:
        overlaps = any(max(start, c_start) < min(end, c_end) for c_start, c_end in claimed_spans)
        if not overlaps:
            claimed_spans.append((start, end))
            matched_fields.add(name)

    return matched_fields


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
        all_field_names = list(EvaluationPayload.model_fields.keys())
        task_field_map: dict[str, str] = {}
        for task in open_tasks:
            if task.agent == "deployment" and task.pr_number is not None:
                named_fields = _match_fields_in_title(task.title, all_field_names)
                for field in named_fields:
                    if field not in task_field_map:
                        task_field_map[field] = str(task.pr_number)

        not_computed: dict[str, str | None] = {}
        for name, field_info in EvaluationPayload.model_fields.items():
            if not field_info.is_required() and getattr(payload, name) is None:
                not_computed[name] = task_field_map.get(name)

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
