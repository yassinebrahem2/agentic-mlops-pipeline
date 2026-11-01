"""SQLite storage and execution of parsers (I22)."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select, update

from mlops_agents.monitoring.results.code_parser import CodeParserRunner
from mlops_agents.monitoring.results.contract import (
    ParseOutcome,
    Parsers,
    ParserSpec,
    ParserVersion,
    parse_source_id,
)
from mlops_agents.monitoring.results.mapping import evaluate_mapping
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import parsers as parsers_table

logger = logging.getLogger(__name__)


class SqliteParsers(Parsers):
    """Parsers store and runner backed by project SQLite database."""

    def __init__(
        self,
        databases: Databases,
        code_runner: CodeParserRunner | None = None,
    ) -> None:
        self._databases = databases
        self._code_runner = code_runner

    def set_code_runner(self, code_runner: CodeParserRunner) -> None:
        self._code_runner = code_runner

    def cycle(self, project_id: str) -> contextlib.AbstractAsyncContextManager[None]:
        """A collection cycle of `project_id`, whose code parser runs share one sandbox
        preparation (CodeParserRunner.cycle)."""
        if self._code_runner is None:
            return contextlib.nullcontext()
        return self._code_runner.cycle(project_id)

    async def store(
        self,
        project_id: str,
        spec: ParserSpec,
        proposal_id: str,
    ) -> ParserVersion:
        """Stores a parser version, idempotent per proposal_id."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            # 1. Check if this proposal_id was already stored
            stmt_prop = select(parsers_table).where(parsers_table.c.proposal_id == proposal_id)
            res_prop = (await conn.execute(stmt_prop)).mappings().first()
            if res_prop is not None:
                return self._row_to_version(res_prop)

            # 2. Get next version number for this source_id
            stmt_ver = (
                select(parsers_table.c.version)
                .where(parsers_table.c.source_id == spec.source_id)
                .order_by(desc(parsers_table.c.version))
                .limit(1)
            )
            max_ver = (await conn.execute(stmt_ver)).scalar_one_or_none()
            next_version = (max_ver or 0) + 1

            now = datetime.now(UTC)
            stmt_ins = parsers_table.insert().values(
                source_id=spec.source_id,
                version=next_version,
                spec=spec.model_dump_json(),
                proposal_id=proposal_id,
                approved=now.isoformat(),
                enabled=1,
                consecutive_failures=0,
            )
            await conn.execute(stmt_ins)

            return ParserVersion(
                source_id=spec.source_id,
                version=next_version,
                spec=spec,
                proposal_id=proposal_id,
                approved=now,
                enabled=True,
                consecutive_failures=0,
            )

    async def current(
        self,
        project_id: str,
        source_id: str,
    ) -> ParserVersion | None:
        """Returns the newest parser version for the source_id."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(parsers_table)
                .where(parsers_table.c.source_id == source_id)
                .order_by(desc(parsers_table.c.version))
                .limit(1)
            )
            row = (await conn.execute(stmt)).mappings().first()
            if row is None:
                return None
            return self._row_to_version(row)

    async def record_failure(
        self,
        project_id: str,
        source_id: str,
        version: int,
        disable_after: int,
    ) -> tuple[int, bool]:
        """Increments consecutive failures and disables version if threshold reached."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt_cur = select(parsers_table.c.consecutive_failures, parsers_table.c.enabled).where(
                parsers_table.c.source_id == source_id,
                parsers_table.c.version == version,
            )
            row = (await conn.execute(stmt_cur)).first()
            if row is None:
                return 0, False

            curr_failures, curr_enabled = row[0], row[1]
            new_failures = curr_failures + 1
            new_enabled = 0 if new_failures >= disable_after else curr_enabled

            stmt_upd = (
                update(parsers_table)
                .where(
                    parsers_table.c.source_id == source_id,
                    parsers_table.c.version == version,
                )
                .values(
                    consecutive_failures=new_failures,
                    enabled=new_enabled,
                )
            )
            await conn.execute(stmt_upd)
            return new_failures, bool(curr_enabled and not new_enabled)

    async def reset_failures(
        self,
        project_id: str,
        source_id: str,
        version: int,
    ) -> None:
        """Resets consecutive failures to 0 on success."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = (
                update(parsers_table)
                .where(
                    parsers_table.c.source_id == source_id,
                    parsers_table.c.version == version,
                    parsers_table.c.enabled == 1,
                )
                .values(consecutive_failures=0)
            )
            await conn.execute(stmt)

    async def parse(
        self,
        project_id: str,
        source_id: str,
        raw: str,
        origin_ref: str,
        *,
        start_offset: int = 0,
        csv_header: list[str] | None = None,
    ) -> ParseOutcome:
        ver = await self.current(project_id, source_id)
        if ver is None:
            raise RuntimeError(f"No parser configured for {source_id}")
        if not ver.enabled:
            raise RuntimeError(f"Parser version {ver.version} for {source_id} is disabled")

        if ver.spec.mapping is not None:
            return evaluate_mapping(
                ver.spec.mapping,
                raw,
                origin_ref,
                start_offset=start_offset,
                csv_header=csv_header,
            )

        if ver.spec.code is not None:
            if self._code_runner is None:
                raise RuntimeError("Code parser runner is not configured")
            conn_name: str | None = None
            with contextlib.suppress(ValueError):
                _, _, conn_name = parse_source_id(source_id)
            return await self._code_runner.run(
                project_id,
                ver.spec.code,
                raw,
                origin_ref,
                connection_name=conn_name,
                start_offset=start_offset,
            )

        raise ValueError("Invalid parser spec: neither mapping nor code is set")

    def _row_to_version(self, row: Any) -> ParserVersion:
        spec_dict = json.loads(row["spec"])
        spec = ParserSpec.model_validate(spec_dict)
        approved_dt = datetime.fromisoformat(row["approved"])
        if approved_dt.tzinfo is None:
            approved_dt = approved_dt.replace(tzinfo=UTC)

        return ParserVersion(
            source_id=row["source_id"],
            version=int(row["version"]),
            spec=spec,
            proposal_id=row["proposal_id"],
            approved=approved_dt,
            enabled=bool(row["enabled"]),
            consecutive_failures=int(row["consecutive_failures"]),
        )
