"""Tracking service implementation (I16)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.credentials.project_secrets import ProjectSecrets, tracking_ref
from mlops_agents.credentials.scrub import error_text, scrub_url_secrets, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings, projects, tracking_sources
from mlops_agents.tracking.constants import (
    DEFAULT_SYNC_INTERVAL_S,
    ERROR_MAX_CHARS,
    MIN_SYNC_INTERVAL_S,
    PRODUCTION_ALIAS,
    PRODUCTION_ENVIRONMENT,
    QUERY_LIMIT_MAX,
    SYNC_INTERVAL_KEY,
)
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TrackingService(Tracking):
    def __init__(
        self,
        databases: Databases,
        keystore: KeyStore,
        secrets: ProjectSecrets,
        events: EventLog,
        adapters: Mapping[SourceType, TrackingAdapter],
        github_token: Callable[[], Awaitable[str | None]],
        handoffs: Handoffs,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._databases = databases
        self._keystore = keystore
        self._secrets = secrets
        self._events = events
        self._adapters: dict[SourceType, TrackingAdapter] = dict(adapters)
        self._github_token = github_token
        self._clock = clock
        self._handoffs = handoffs
        self._handoff_error_logged: bool = False

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
            raise DomainError(
                "tracking_source_not_found",
                f"Tracking source '{source_id}' does not exist.",
                status=404,
            )

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
            raise DomainError(
                "tracking_source_not_found",
                f"Tracking source '{source_id}' does not exist.",
                status=404,
            )
        return await row_to_source_view(updated_row, project_id, self._secrets)

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        """Starts a sync in the background; tracking.synced reports its end."""
        await self._check_project(project_id)
        if source_id is not None:
            engine = await self._databases.project(project_id)
            async with engine.connect() as conn:
                row = await load_source_row(conn, source_id)
            if row is None:
                raise DomainError(
                    "tracking_source_not_found",
                    f"Tracking source '{source_id}' does not exist.",
                    status=404,
                )
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

        all_sources = await self.sources(project_id)
        if not all_sources:
            return

        await asyncio.gather(
            *(self._sync_source(project_id, s.id) for s in all_sources),
            return_exceptions=True,
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
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = await load_source_row(conn, source_id)
        if row is None:
            return

        source = await row_to_source_view(row, project_id, self._secrets)
        credential: str | None = None

        try:
            if source.auth == "credential":
                ref = tracking_ref(project_id, source.id)
                credential = await asyncio.to_thread(self._keystore.get, ref)
                if credential is None:
                    await self._record_sync_error(
                        project_id,
                        source,
                        "The credential is missing from the key store; set it again",
                        credential=None,
                    )
                    return
            elif source.type == "github_actions":
                credential = await self._github_token()

            adapter = self._adapters.get(source.type)
            if adapter is None:
                await self._record_sync_error(
                    project_id, source, "No adapter for this source type", credential=credential
                )
                return

            result = await adapter.sync(source, credential, since=source.last_sync)
            if result.error is not None:
                await self._record_sync_error(
                    project_id, source, result.error, credential=credential
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
            await self._events.record(
                project_id,
                source="system",
                type="tracking.synced",
                subject_id=source.id,
                payload=payload,
            )
        except Exception as exc:
            err = error_text(exc)
            await self._record_sync_error(project_id, source, err, credential=credential)

    async def _record_sync_error(
        self,
        project_id: str,
        source: TrackingSourceView,
        error_msg: str,
        credential: str | None,
    ) -> None:
        secrets_to_scrub = [credential] if credential else []
        try:
            gh_token = await self._github_token()
            if gh_token:
                secrets_to_scrub.append(gh_token)
        except Exception:
            _log.debug("Could not resolve github token for error scrubbing")
        scrubbed = scrub_url_secrets(error_msg)
        scrubbed = scrub_values(scrubbed, secrets_to_scrub)
        if len(scrubbed) > ERROR_MAX_CHARS:
            scrubbed = scrubbed[:ERROR_MAX_CHARS]

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(tracking_sources)
                .where(tracking_sources.c.id == source.id)
                .values(last_error=scrubbed)
            )

        payload = {
            "source_id": source.id,
            "type": source.type,
            "ok": False,
            "versions": 0,
            "runs": 0,
            "metrics": 0,
            "ci_runs": 0,
            "evaluations": 0,
            "error": scrubbed,
        }
        await self._events.record(
            project_id,
            source="system",
            type="tracking.synced",
            subject_id=source.id,
            payload=payload,
        )

    async def _resolve_candidate_aliases_fn(
        self, project_id: str
    ) -> Callable[[str], Sequence[str]]:
        handoff: HandoffView | None = None
        try:
            handoff = await self._handoffs.current(project_id)
        except Exception as exc:
            if not self._handoff_error_logged:
                _log.warning("Failed to read handoff for project %s: %s", project_id, exc)
                self._handoff_error_logged = True
            return lambda _model: [PRODUCTION_ALIAS]

        if handoff is None or not handoff.payload.models:
            return lambda _model: [PRODUCTION_ALIAS]

        specs_by_name = {m.name: m for m in handoff.payload.models}

        def _get_candidates(model_name: str) -> Sequence[str]:
            spec = specs_by_name.get(model_name)
            if spec is None:
                return [PRODUCTION_ALIAS]
            candidates = sorted(
                alias for alias, env in spec.environments.items() if env == PRODUCTION_ENVIRONMENT
            )
            return candidates if candidates else [PRODUCTION_ALIAS]

        return _get_candidates

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        await self._check_project(project_id)

        if q.limit > QUERY_LIMIT_MAX or q.limit < 1:
            raise DomainError(
                "tracking_bad_query",
                f"Query limit must be between 1 and {QUERY_LIMIT_MAX}.",
                status=422,
            )
        if q.offset < 0:
            raise DomainError(
                "tracking_bad_query", "Query offset must be non-negative.", status=422
            )

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            if q.what == "models":
                candidate_fn = await self._resolve_candidate_aliases_fn(project_id)
                return await query_models(conn, q, candidate_fn)
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

    async def sync_interval(self, project_id: str) -> int:
        await self._check_project(project_id)
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == SYNC_INTERVAL_KEY
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return DEFAULT_SYNC_INTERVAL_S
        try:
            val = int(json.loads(str(row)))
            return val if val >= MIN_SYNC_INTERVAL_S else DEFAULT_SYNC_INTERVAL_S
        except (ValueError, TypeError, json.JSONDecodeError):
            return DEFAULT_SYNC_INTERVAL_S

    async def set_sync_interval(self, project_id: str, interval_s: int) -> int:
        await self._check_project(project_id)
        if interval_s < MIN_SYNC_INTERVAL_S:
            raise DomainError(
                "tracking_invalid_interval",
                f"Sync interval must be at least {MIN_SYNC_INTERVAL_S} seconds.",
                status=422,
            )
        engine = await self._databases.project(project_id)
        encoded = json.dumps(interval_s)
        async with engine.begin() as conn:
            await conn.execute(
                sqlite_insert(project_settings)
                .values(key=SYNC_INTERVAL_KEY, value=encoded)
                .on_conflict_do_update(index_elements=["key"], set_={"value": encoded})
            )
        return interval_s

    async def close(self) -> None:
        """Cancels background sync tasks."""
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
