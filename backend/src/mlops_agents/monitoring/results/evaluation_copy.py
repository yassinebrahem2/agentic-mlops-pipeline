"""SqliteEvaluationCopies: stores evaluation copies from results location (I22 -> I37)."""

from __future__ import annotations

import json

from sqlalchemy import select, update

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.results.contract import EvaluationCopies
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import evaluations, model_versions
from mlops_agents.tracking.contract import EvaluationRecord


class SqliteEvaluationCopies(EvaluationCopies):
    """Stores evaluation copies in evaluations table for fetch_failed model versions."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def store_copy(self, project_id: str, record: EvaluationRecord) -> bool:
        """Stores evaluation copy if model version exists and has evaluation='fetch_failed'.

        Returns True if stored, False if skipped or already present.
        """
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = select(model_versions).where(
                model_versions.c.model == record.model,
                model_versions.c.version == record.version,
            )
            res = await conn.execute(stmt)
            mv_rows = res.mappings().all()
            if not mv_rows:
                return False

            matching_mv = next((r for r in mv_rows if r["evaluation"] == "fetch_failed"), None)
            if matching_mv is None:
                return False

            target_source_id = matching_mv["source_id"]

            # Check if row already exists in evaluations table
            eval_stmt = select(evaluations).where(
                evaluations.c.source_id == target_source_id,
                evaluations.c.model == record.model,
                evaluations.c.version == record.version,
            )
            eval_res = await conn.execute(eval_stmt)
            if eval_res.first() is not None:
                return False

            # Insert into evaluations table
            run_key = record.run_key or matching_mv["run_key"] or ""
            ins_stmt = evaluations.insert().values(
                source_id=target_source_id,
                model=record.model,
                version=record.version,
                run_key=run_key,
                schema_version=record.schema_version,
                payload=json.dumps(record.payload),
                fetched_at=to_iso(record.fetched_at),
            )
            await conn.execute(ins_stmt)

            # Update model_versions evaluation status to "present"
            upd_stmt = (
                update(model_versions)
                .where(
                    model_versions.c.source_id == target_source_id,
                    model_versions.c.model == record.model,
                    model_versions.c.version == record.version,
                )
                .values(evaluation="present")
            )
            await conn.execute(upd_stmt)
            return True
