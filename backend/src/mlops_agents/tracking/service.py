"""Tracking service implementation (I16)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select, update

from mlops_agents.clock import utc_now
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.credentials.project_secrets import ProjectSecrets, tracking_ref
from mlops_agents.credentials.scrub import error_text, scrub_url_secrets, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects, tracking_sources
from mlops_agents.tracking.constants import ERROR_MAX_CHARS
from mlops_agents.tracking.contract import (
    Page,
    SourceType,
    Tracking,
    TrackingAdapter,
    TrackingQuery,
    TrackingSourceView,
)
from mlops_agents.tracking.sources import (
    insert_source_row,
    load_all_source_rows,
    load_source_row,
    row_to_source_view,
    validate_source_input,
)
from mlops_agents.tracking.store import (
    query_ci_runs,
    query_deployments,
    query_evaluations,
    query_metrics,
    query_models,
    query_runs,
    query_versions,
    save_sync_result,
)

_log = logging.getLogger(__name__)


def _source_not_found(source_id: str) -> DomainError:
    return DomainError(
        "tracking_source_not_found",
        f"Tracking source '{source_id}' does not exist.",
        status=404,
    )


class TrackingService(Tracking):
    def __init__(
        self,
        databases: Databases,
        keystore: KeyStore,
        secrets: ProjectSecrets,
        events: EventLog,
        adapters: Mapping[SourceType, TrackingAdapter],
        github_token: Callable[[], Awaitable[str | None]],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._databases = databases
        self._keystore = keystore
        self._secrets = secrets
        self._events = events
        self._adapters: dict[SourceType, TrackingAdapter] = dict(adapters)
        self._github_token = github_token
        self._clock = clock

        self._source_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task[None]] = set()

    async def _check_project(self, project_id: str) -> None:
        try:
            self._databases.paths.project_dir(project_id)
        except ValueError:
            raise DomainError(
                "project_not_found", "This project does not exist.", status=404
            ) from None

        engine = await self._databases.app()
        async with engine.connect() as conn:
            exists = (
                await conn.execute(select(projects.c.id).where(projects.c.id == project_id))
            ).one_or_none()
        if exists is None:
            raise DomainError("project_not_found", "This project does not exist.", status=404)

    async def _get_source_lock(self, project_id: str, source_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            key = (project_id, source_id)
            lock = self._source_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._source_locks[key] = lock
            return lock

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        await self._check_project(project_id)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = await load_all_source_rows(conn)
            views: list[TrackingSourceView] = []
            for row in rows:
                views.append(await row_to_source_view(row, project_id, self._secrets))
            return views

    async def create_source(
        self,
        project_id: str,
        type: SourceType,
        settings: dict[str, Any],
        auth: Literal["none", "credential"] = "none",
    ) -> TrackingSourceView:
        await self._check_project(project_id)
        source_type, validated_settings, auth_mode = validate_source_input(type, settings, auth)

        source_id = new_id("trk")
        created_at_iso = self._clock().isoformat()

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await insert_source_row(
                conn,
                source_id=source_id,
                type_=source_type,
                settings=validated_settings,
                auth=auth_mode,
                created_at=created_at_iso,
            )

        return TrackingSourceView(
            id=source_id,
            type=source_type,
            settings=validated_settings,
            auth=auth_mode,
            credential_set=False,
            last_sync=None,
            last_error=None,
        )

    async def set_credential(
        self, project_id: str, source_id: str, credential: str
    ) -> TrackingSourceView:
        await self._check_project(project_id)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = await load_source_row(conn, source_id)
        if row is None:
            raise _source_not_found(source_id)

        if row["auth"] != "credential":
            raise DomainError(
                "tracking_no_credential_needed",
                "This source does not take a credential.",
                status=422,
            )

        ref = tracking_ref(project_id, source_id)
        await self._secrets.put(ref, credential)

        async with engine.connect() as conn:
            updated_row = await load_source_row(conn, source_id)
        if updated_row is None:
            raise _source_not_found(source_id)
        return await row_to_source_view(updated_row, project_id, self._secrets)

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        """Starts a sync in the background; tracking.synced reports its end."""
        await self._check_project(project_id)
        if source_id is not None:
            engine = await self._databases.project(project_id)
            async with engine.connect() as conn:
                row = await load_source_row(conn, source_id)
            if row is None:
                raise _source_not_found(source_id)
        task = asyncio.create_task(self.run_sync(project_id, source_id, check_project=False))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def run_sync(
        self, project_id: str, source_id: str | None = None, *, check_project: bool = True
    ) -> None:
        """Runs the sync work, awaited."""
        if check_project:
            await self._check_project(project_id)
        if source_id is not None:
            await self._sync_source(project_id, source_id)
            return

        try:
            all_sources = await self.sources(project_id)
        except Exception as exc:
            _log.warning(
                "Could not list sources for sync in project %s: %s", project_id, error_text(exc)
            )
            return
        if not all_sources:
            return

        results = await asyncio.gather(
            *(self._sync_source(project_id, s.id) for s in all_sources),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, Exception):
                _log.warning(
                    "Sync failed for source in project %s: %s", project_id, error_text(res)
                )

    async def _sync_source(self, project_id: str, source_id: str) -> None:
        lock = await self._get_source_lock(project_id, source_id)
        if lock.locked():
            # Already syncing: await running sync and return without running a second one
            async with lock:
                return

        async with lock:
            await self._do_sync_source(project_id, source_id)

    async def _do_sync_source(self, project_id: str, source_id: str) -> None:
        source_type: str = "unknown"
        credential: str | None = None
        try:
            engine = await self._databases.project(project_id)
            async with engine.connect() as conn:
                row = await load_source_row(conn, source_id)
            if row is None:
                return

            source = await row_to_source_view(row, project_id, self._secrets)
            source_type = source.type

            if source.auth == "credential":
                ref = tracking_ref(project_id, source.id)
                credential = await asyncio.to_thread(self._keystore.get, ref)
                if credential is None:
                    await self._record_sync_error(
                        project_id,
                        source.id,
                        source.type,
                        "The credential is missing from the key store; set it again",
                        credential=None,
                    )
                    return
            elif source.type == "github_actions":
                credential = await self._github_token()

            adapter = self._adapters.get(source.type)
            if adapter is None:
                await self._record_sync_error(
                    project_id,
                    source.id,
                    source.type,
                    "No adapter for this source type",
                    credential=credential,
                )
                return

            result = await adapter.sync(source, credential, since=source.last_sync)
            if result.error is not None:
                await self._record_sync_error(
                    project_id, source.id, source.type, result.error, credential=credential
                )
                return

            # Success: write in one transaction
            now_dt = self._clock()
            async with engine.begin() as conn:
                await save_sync_result(conn, source.id, result, now_dt)

            # Record tracking.synced event
            payload = {
                "source_id": source.id,
                "type": source.type,
                "ok": True,
                "versions": len(result.versions),
                "runs": len(result.runs),
                "metrics": len(result.metrics),
                "ci_runs": len(result.ci_runs),
                "evaluations": len(result.evaluations),
                "error": None,
            }
            try:
                await self._events.record(
                    project_id,
                    source="system",
                    type="tracking.synced",
                    subject_id=source.id,
                    payload=payload,
                )
            except Exception as ev_exc:
                _log.warning("Could not record tracking.synced event: %s", error_text(ev_exc))
        except Exception as exc:
            err = error_text(exc)
            await self._record_sync_error(
                project_id, source_id, source_type, err, credential=credential
            )

    async def _record_sync_error(
        self,
        project_id: str,
        source_id: str,
        source_type: str,
        error_msg: str,
        credential: str | None,
    ) -> None:
        secrets_to_scrub = [credential] if credential else []
        try:
            gh_token = await self._github_token()
            if gh_token:
                secrets_to_scrub.append(gh_token)
        except Exception as exc:
            _log.warning("Could not resolve github token for error scrubbing: %s", error_text(exc))
        scrubbed = scrub_url_secrets(error_msg)
        scrubbed = scrub_values(scrubbed, secrets_to_scrub)
        if len(scrubbed) > ERROR_MAX_CHARS:
            scrubbed = scrubbed[:ERROR_MAX_CHARS]

        try:
            engine = await self._databases.project(project_id)
            async with engine.begin() as conn:
                await conn.execute(
                    update(tracking_sources)
                    .where(tracking_sources.c.id == source_id)
                    .values(last_error=scrubbed)
                )
        except Exception as exc:
            _log.warning(
                "Could not update last_error for source %s: %s", source_id, error_text(exc)
            )

        payload = {
            "source_id": source_id,
            "type": source_type,
            "ok": False,
            "versions": 0,
            "runs": 0,
            "metrics": 0,
            "ci_runs": 0,
            "evaluations": 0,
            "error": scrubbed,
        }
        try:
            await self._events.record(
                project_id,
                source="system",
                type="tracking.synced",
                subject_id=source_id,
                payload=payload,
            )
        except Exception as exc:
            _log.warning("Could not record tracking.synced error event: %s", error_text(exc))

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        await self._check_project(project_id)

        if q.what == "metrics" and not q.run_key:
            raise DomainError(
                "tracking_bad_query", "Querying metrics requires run_key.", status=422
            )

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            if q.what == "models":
                return await query_models(conn, q)
            if q.what == "versions":
                return await query_versions(conn, q)
            if q.what == "runs":
                return await query_runs(conn, q)
            if q.what == "metrics":
                return await query_metrics(conn, q)
            if q.what == "deployments":
                return await query_deployments(conn, q)
            if q.what == "ci_runs":
                return await query_ci_runs(conn, q)
            if q.what == "evaluations":
                return await query_evaluations(conn, q)

        raise DomainError("tracking_bad_query", f"Unknown query target: {q.what!r}.", status=422)

    async def close(self) -> None:
        """Cancels background sync tasks."""
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
