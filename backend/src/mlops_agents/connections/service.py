"""Service managing project data connections."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.exc import IntegrityError

from mlops_agents.clock import now_iso
from mlops_agents.connections.contract import (
    CONNECTION_NAME_PATTERN,
    ConnectionKind,
    ConnectionStatus,
    ConnectionView,
    DuckDbSource,
    Findings,
    Mount,
    TableRef,
)
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.credentials.scrub import error_text
from mlops_agents.errors import DomainError, validation_error_message
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import connections


class ConnectionsService:
    """Manages connections for projects."""

    def __init__(
        self,
        databases: Databases,
        events: SqliteEventLog,
        registry: KindRegistry,
    ) -> None:
        self._databases = databases
        self._events = events
        self._registry = registry

    async def _get_row(self, project_id: str, connection_id: str) -> RowMapping:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(connections).where(connections.c.id == connection_id)))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise DomainError(
                "connection_not_found",
                f"Connection '{connection_id}' not found.",
                status=404,
            )
        return row

    def _parse(self, kind: ConnectionKind, settings: dict[str, Any]) -> BaseModel:
        try:
            return kind.settings_model.model_validate(settings)
        except ValidationError as err:
            message = validation_error_message(err)
            raise DomainError(
                "connection_invalid",
                f"The settings are invalid ({message.text}).",
                status=422,
                detail={"errors": message.errors},
            ) from None

    def _stored(self, settings_json: Any) -> dict[str, Any]:
        """A row's stored settings, for `_parse`; never the text of the decode error."""
        # SQLite is dynamically typed: a hand-edited row can hold a blob or a number.
        if not isinstance(settings_json, str):
            raise DomainError(
                "connection_invalid", "The stored settings are not valid JSON.", status=422
            )
        try:
            value = json.loads(settings_json)
        except ValueError:
            raise DomainError(
                "connection_invalid", "The stored settings are not valid JSON.", status=422
            ) from None
        if not isinstance(value, dict):
            raise DomainError(
                "connection_invalid", "The stored settings are not a JSON object.", status=422
            )
        return cast(dict[str, Any], value)

    async def _load(self, project_id: str, connection_id: str) -> tuple[ConnectionKind, BaseModel]:
        row = await self._get_row(project_id, connection_id)
        kind_obj = self._registry.get(row["kind"])
        return kind_obj, self._parse(kind_obj, self._stored(row["settings_json"]))

    async def _read(self, kind: ConnectionKind, parsed: BaseModel) -> tuple[Findings, str | None]:
        findings = await asyncio.to_thread(kind.findings, parsed)
        data_ver = await asyncio.to_thread(kind.latest_data_version, parsed)
        return findings, data_ver

    async def inspect(self, project_id: str, kind: str, settings: dict[str, Any]) -> Findings:
        del project_id
        kind_obj = self._registry.get(kind)
        parsed = self._parse(kind_obj, settings)
        await asyncio.to_thread(kind_obj.validate, parsed, None)
        return await asyncio.to_thread(kind_obj.findings, parsed)

    async def create(
        self, project_id: str, kind: str, name: str, settings: dict[str, Any]
    ) -> ConnectionView:
        kind_obj = self._registry.get(kind)

        if re.fullmatch(CONNECTION_NAME_PATTERN, name) is None:
            raise DomainError(
                "invalid_connection_name",
                f"Connection name '{name}' is invalid.",
                status=400,
            )

        parsed = self._parse(kind_obj, settings)
        await asyncio.to_thread(kind_obj.validate, parsed, None)
        # Read before the insert: a folder that fails here leaves no row behind.
        findings, data_ver = await self._read(kind_obj, parsed)
        stored = parsed.model_dump(mode="json")

        con_id = new_id("con")
        now = now_iso()
        engine = await self._databases.project(project_id)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(connections).values(
                        id=con_id,
                        name=name,
                        kind=kind,
                        settings_json=json.dumps(stored),
                        credential_ref=None,
                        status="ok",
                        status_detail=None,
                        created=now,
                        updated=now,
                    )
                )
        except IntegrityError:
            raise DomainError(
                "connection_name_taken",
                f"A connection named '{name}' already exists in this project.",
                status=409,
            ) from None

        await self._events.record(
            project_id,
            source="user",
            type="connection.created",
            subject_id=con_id,
            payload={"name": name, "kind": kind},
        )

        return ConnectionView(
            id=con_id,
            name=name,
            kind=kind,
            settings=stored,
            status="ok",
            status_detail=None,
            findings=findings,
            data_version=data_ver,
        )

    async def update(
        self, project_id: str, connection_id: str, settings: dict[str, Any]
    ) -> ConnectionView:
        row = await self._get_row(project_id, connection_id)
        kind_obj = self._registry.get(row["kind"])
        parsed = self._parse(kind_obj, settings)
        await asyncio.to_thread(kind_obj.validate, parsed, None)
        findings, data_ver = await self._read(kind_obj, parsed)
        stored = parsed.model_dump(mode="json")

        now = now_iso()
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(connections)
                .where(connections.c.id == connection_id)
                .values(
                    settings_json=json.dumps(stored),
                    status="ok",
                    status_detail=None,
                    updated=now,
                )
            )

        await self._events.record(
            project_id,
            source="user",
            type="connection.updated",
            subject_id=connection_id,
            payload={"name": row["name"], "kind": row["kind"]},
        )

        return ConnectionView(
            id=connection_id,
            name=row["name"],
            kind=row["kind"],
            settings=stored,
            status="ok",
            status_detail=None,
            findings=findings,
            data_version=data_ver,
        )

    async def delete(self, project_id: str, connection_id: str) -> None:
        row = await self._get_row(project_id, connection_id)
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(sql_delete(connections).where(connections.c.id == connection_id))

        await self._events.record(
            project_id,
            source="user",
            type="connection.deleted",
            subject_id=connection_id,
            payload={"name": row["name"], "kind": row["kind"]},
        )

    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]:
        kind_obj, parsed = await self._load(project_id, connection_id)
        return await asyncio.to_thread(kind_obj.tables, parsed)

    async def data_version(self, project_id: str, connection_id: str, table: str) -> str:
        kind_obj, parsed = await self._load(project_id, connection_id)
        tables = await asyncio.to_thread(kind_obj.tables, parsed)
        matching = next((t for t in tables if t.name == table), None)
        if matching is None:
            raise DomainError(
                "table_not_found",
                f"Table '{table}' not found.",
                status=404,
            )

        return await asyncio.to_thread(kind_obj.data_version, parsed, matching)

    async def duckdb_source(self, project_id: str, connection_id: str, table: str) -> DuckDbSource:
        kind_obj, parsed = await self._load(project_id, connection_id)
        tables = await asyncio.to_thread(kind_obj.tables, parsed)
        matching = next((t for t in tables if t.name == table), None)
        if matching is None:
            raise DomainError(
                "table_not_found",
                f"Table '{table}' not found.",
                status=404,
            )

        return await asyncio.to_thread(kind_obj.duckdb_source, parsed, matching)

    async def mounts(self, project_id: str) -> list[Mount]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(connections))).all()

        mounts_list: list[Mount] = []
        for row in rows:
            try:
                kind_obj = self._registry.get(row.kind)
                parsed = self._parse(kind_obj, self._stored(row.settings_json))
                await asyncio.to_thread(kind_obj.validate, parsed, None)
                m = await asyncio.to_thread(kind_obj.sandbox_mount, row.name, parsed)
                if m is not None:
                    mounts_list.append(m)
            except (DomainError, OSError) as exc:
                raise DomainError(
                    "connection_unreachable",
                    f"Connection '{row.name}' is unreachable: {error_text(exc)}",
                    status=400,
                ) from None  # the cause's own text is not scrubbed

        mounts_list.sort(key=lambda m: m.container_path)
        return mounts_list

    async def list(self, project_id: str) -> list[ConnectionView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(connections).order_by(connections.c.created))).all()

        views: list[ConnectionView] = []
        for row in rows:
            status: ConnectionStatus = "ok"
            status_detail: str | None = None
            findings = Findings(unsupported_files=[], secret_files=[], truncated=False)
            data_ver: str | None = None
            settings: dict[str, Any] = {}

            try:
                raw_settings = self._stored(row.settings_json)
                kind_obj = self._registry.get(row.kind)
                parsed = self._parse(kind_obj, raw_settings)
                # Only parsed settings are shown: a stored value the model refuses stays hidden.
                settings = parsed.model_dump(mode="json")
                await asyncio.to_thread(kind_obj.validate, parsed, None)
                findings, data_ver = await self._read(kind_obj, parsed)
            except (DomainError, OSError) as exc:
                # The kind's own failures; anything else is a bug and reaches the 500 handler.
                status = "unreachable"
                status_detail = error_text(exc)

            if status != row.status or status_detail != row.status_detail:
                async with engine.begin() as conn:
                    await conn.execute(
                        update(connections)
                        .where(connections.c.id == row.id)
                        .values(status=status, status_detail=status_detail)
                    )

            views.append(
                ConnectionView(
                    id=row.id,
                    name=row.name,
                    kind=row.kind,
                    settings=settings,
                    status=status,
                    status_detail=status_detail,
                    findings=findings,
                    data_version=data_ver,
                )
            )

        return views
