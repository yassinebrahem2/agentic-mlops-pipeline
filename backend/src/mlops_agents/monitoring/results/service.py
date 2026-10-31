"""ResultsService implementation and collection loop (I22)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from mlops_agents.connections.contract import Connections
from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import Maintenance, NeverSuspended
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.results.ci_source import CiSourceReader
from mlops_agents.monitoring.results.contract import (
    CollectionReport,
    EvaluationCopies,
    ParseOutcome,
    Parsers,
    ResultQuery,
    ResultRecord,
    Results,
    ResultsObserver,
    SourceKind,
    SourceState,
    StoredResult,
    parse_source_id,
    source_id,
)
from mlops_agents.monitoring.results.evaluation_copy import SqliteEvaluationCopies
from mlops_agents.monitoring.results.file_source import FileSourceReader, SourceReadOutcome
from mlops_agents.monitoring.results.mapping import incomplete_record_error
from mlops_agents.monitoring.results.mlflow_source import MlflowSourceReader
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import CiResults, FileResults, JobFeedRef
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page, Tracking

if TYPE_CHECKING:
    from mlops_agents.monitoring.expectations.checks import ChecksStore

logger = logging.getLogger(__name__)


class SourceReader(Protocol):
    """Protocol for reading a source kind."""

    async def read(
        self,
        project_id: str,
        source_id: str,
        cursor: dict[str, object],
        *,
        consecutive_failures: int = 0,
    ) -> SourceReadOutcome: ...


class SignalSink(Protocol):
    """Protocol for raising signals into Incidents."""

    async def raise_signal(self, project_id: str, draft: SignalDraft, seen_at: datetime) -> Any: ...


class ResultsService(Results):
    """Main results service managing collection, storage, delivery and queries."""

    def __init__(
        self,
        databases: Databases,
        handoffs: Handoffs,
        connections: Connections,
        events: SqliteEventLog,
        *,
        tracking: Tracking | None = None,
        store: ResultsStore | None = None,
        file_reader: FileSourceReader | None = None,
        ci_reader: CiSourceReader | None = None,
        mlflow_reader: MlflowSourceReader | None = None,
        evaluation_copies: EvaluationCopies | None = None,
        incidents: SignalSink | None = None,
        maintenance: Maintenance | None = None,
        parsers: Parsers | None = None,
        checks: ChecksStore | None = None,
    ) -> None:
        self._databases = databases
        self._handoffs = handoffs
        self._connections = connections
        self._events = events
        self._tracking = tracking
        self._store = store or ResultsStore(databases)
        self._file_reader = file_reader or FileSourceReader(connections)
        self._ci_reader = ci_reader or (CiSourceReader(tracking, handoffs) if tracking else None)
        self._mlflow_reader = mlflow_reader or (
            MlflowSourceReader(tracking, handoffs) if tracking else None
        )
        self._evaluation_copies = evaluation_copies or SqliteEvaluationCopies(databases)
        self._incidents = incidents
        self._maintenance = maintenance or NeverSuspended()
        self._parsers = parsers or SqliteParsers(databases)
        self._checks = checks
        self._observers: list[ResultsObserver] = []
        self._delivery_locks: dict[str, asyncio.Lock] = {}

    @property
    def parsers(self) -> Parsers:
        return self._parsers

    def bind_checks(self, checks: ChecksStore) -> None:
        """Binds checks store to results service."""
        self._checks = checks

    def bind_incidents(self, incidents: SignalSink) -> None:
        """Binds incidents service to results service."""
        self._incidents = incidents

    def bind_tracking(self, tracking: Tracking) -> None:
        """Binds tracking service to results service and initializes tracker readers."""
        self._tracking = tracking
        if self._ci_reader is None:
            self._ci_reader = CiSourceReader(tracking, self._handoffs)
        if self._mlflow_reader is None:
            self._mlflow_reader = MlflowSourceReader(tracking, self._handoffs)

    def bind_maintenance(self, maintenance: Maintenance) -> None:
        """Binds maintenance service to results service."""
        self._maintenance = maintenance

    @property
    def maintenance(self) -> Maintenance:
        return self._maintenance

    @property
    def store(self) -> ResultsStore:
        return self._store

    def add_observer(self, observer: ResultsObserver) -> None:
        self._observers.append(observer)

    def _delivery_lock(self, project_id: str) -> asyncio.Lock:
        """The project's delivery lock. Held from reading undelivered rows to marking them,
        so two deliveries for one project never hand the same rows to the observers."""
        lock = self._delivery_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._delivery_locks[project_id] = lock
        return lock

    async def deliver_undelivered(self, project_id: str, now: datetime) -> None:
        """Delivers any previously committed but undelivered results to observers."""
        async with self._delivery_lock(project_id):
            await self._deliver_undelivered_locked(project_id, now)

    async def _deliver_undelivered_locked(self, project_id: str, now: datetime) -> None:
        """`deliver_undelivered` for a caller already holding the project's delivery lock."""
        if not self._observers:
            return

        undelivered = await self._store.fetch_undelivered(project_id)
        if not undelivered:
            return

        for observer in self._observers:
            try:
                await observer.stored(project_id, undelivered)
            except Exception:
                logger.exception(
                    "ResultsObserver %r raised during delivery for project %s",
                    observer,
                    project_id,
                )

        await self._store.mark_delivered(project_id, [r.id for r in undelivered], delivered_at=now)

    async def _derive_sources(self, project_id: str) -> list[tuple[str, SourceKind, bool]]:
        """Derives sources from the current handoff.

        Returns tuples of (source_id, kind, needs_parser).
        """
        derived: dict[str, tuple[SourceKind, bool]] = {}

        def _add_source(s_id: str, kind: SourceKind, needs_parser: bool) -> None:
            if s_id in derived:
                cur_kind, cur_parser = derived[s_id]
                derived[s_id] = (cur_kind, cur_parser or needs_parser)
            else:
                derived[s_id] = (kind, needs_parser)

        handoff = await self._handoffs.current(project_id)
        if handoff is not None:
            ops = handoff.payload.operations
            # Standard results location
            if isinstance(ops.results_location, FileResults):
                s_id = source_id(
                    "file",
                    ops.results_location.path_pattern,
                    ops.results_location.connection,
                )
                _add_source(s_id, "file", False)
            else:
                s_id = source_id("mlflow", ops.results_location.metric)
                _add_source(s_id, "mlflow", False)

            # Standard check specs
            for check in ops.checks:
                if isinstance(check.results, FileResults):
                    s_id = source_id(
                        "file",
                        check.results.path_pattern,
                        check.results.connection,
                    )
                    _add_source(s_id, "file", False)
                elif isinstance(check.results, CiResults):
                    s_id = source_id("ci", check.results.workflow)
                    _add_source(s_id, "ci", False)
                else:
                    s_id = source_id("mlflow", check.results.metric)
                    _add_source(s_id, "mlflow", False)

            # Team job feeds (file needs a parser; ci_workflow does not)
            for feed in ops.feeds:
                if isinstance(feed.ref, JobFeedRef):
                    if feed.ref.file is not None:
                        s_id = source_id(
                            "file",
                            feed.ref.file.path_pattern,
                            feed.ref.file.connection,
                        )
                        _add_source(s_id, "file", True)
                    elif feed.ref.ci_workflow is not None:
                        s_id = source_id("ci", feed.ref.ci_workflow)
                        _add_source(s_id, "ci", False)

        if self._checks is not None:
            active_checks = await self._checks.list_active(project_id)
            for chk in active_checks:
                if isinstance(chk.results_location, FileResults):
                    s_id = source_id(
                        "file",
                        chk.results_location.path_pattern,
                        chk.results_location.connection,
                    )
                    _add_source(s_id, "file", False)
                else:
                    s_id = source_id("mlflow", chk.results_location.metric)
                    _add_source(s_id, "mlflow", False)

        return [(s_id, kind, needs_parser) for s_id, (kind, needs_parser) in derived.items()]

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        """Runs a collection cycle for all derived sources."""
        # The rows a cycle stores stay undelivered until its own delivery marks them, so the
        # whole cycle holds the project's delivery lock.
        async with self._delivery_lock(project_id), self._parser_cycle(project_id):
            report = await self._collect_locked(project_id, now)

        # 5. Emit results.collected event
        await self._events.record(
            project_id,
            source="results",
            type="results.collected",
            payload=report.model_dump(),
        )

        return report

    def _parser_cycle(self, project_id: str) -> contextlib.AbstractAsyncContextManager[None]:
        """The parsers' scope for one collection cycle, so its code parser runs prepare the
        sandbox once."""
        if isinstance(self._parsers, SqliteParsers):
            return self._parsers.cycle(project_id)
        return contextlib.nullcontext()

    async def _collect_locked(self, project_id: str, now: datetime) -> CollectionReport:
        """Steps 1 to 4 of `collect`, for a caller holding the project's delivery lock."""
        # 1. Deliver any undelivered records first
        await self._deliver_undelivered_locked(project_id, now)

        # 2. Derive sources
        sources = await self._derive_sources(project_id)

        # 3. Read every source oldest first (by last_read timestamp)
        source_states: list[tuple[str, SourceKind, bool, SourceState | None]] = []
        for s_id, kind, needs_parser in sources:
            st = await self._store.get_source_state(project_id, s_id)
            source_states.append((s_id, kind, needs_parser, st))

        def _sort_key(
            item: tuple[str, SourceKind, bool, SourceState | None],
        ) -> tuple[int, datetime, str]:
            _s_id, _k, _p, st = item
            if st is None or st.last_read is None:
                return 0, datetime.min.replace(tzinfo=UTC), _s_id
            return 1, st.last_read, _s_id

        source_states.sort(key=_sort_key)

        stored_total = 0
        by_source: dict[str, int] = {}
        failures: dict[str, str] = {}
        all_newly_stored: list[StoredResult] = []

        for s_id, kind, needs_parser, state in source_states:
            current_cursor = state.cursor if state is not None else {}

            if needs_parser:
                cur_parser = await self._parsers.current(project_id, s_id)
                if cur_parser is None or not cur_parser.enabled:
                    if state is None:
                        await self._store.save_source_state(
                            project_id,
                            SourceState(
                                source_id=s_id,
                                kind=kind,
                                cursor={},
                                last_read=now,
                                last_error=None,
                                consecutive_failures=0,
                            ),
                        )
                    by_source[s_id] = 0
                    continue

                source_stored = 0
                if kind == "file":
                    raw_read = await self._file_reader.read_raw(
                        project_id,
                        s_id,
                        cursor=current_cursor,
                        now=now,
                        mapping=cur_parser.spec.mapping,
                    )
                    if raw_read.last_error:
                        failures[s_id] = raw_read.last_error
                        prev_err = state.last_error if state else None
                        if not prev_err and self._incidents is not None:
                            try:
                                _k, _ref, conn_name = parse_source_id(s_id)
                                scope = conn_name if conn_name is not None else s_id
                            except ValueError:
                                scope = s_id
                            is_suspended = await self._maintenance.suspended(project_id, scope, now)
                            if not is_suspended:
                                draft = SignalDraft(
                                    kind="source_failure",
                                    category="operational",
                                    subject=s_id,
                                    ran_at=now,
                                    detail={"error": raw_read.last_error, "source_id": s_id},
                                    source_ref=s_id,
                                )
                                await self._incidents.raise_signal(project_id, draft, seen_at=now)

                    # A file the reader held (no chunk this cycle) takes the reader's entry, with
                    # its size and mtime as last seen. A file with a chunk advances only once
                    # its chunk parses.
                    chunk_files = {c.rel_posix for c in raw_read.chunks}
                    held_files_cur = dict(current_cursor.get("files", {}))
                    for rel_posix, entry in raw_read.cursor.get("files", {}).items():
                        if rel_posix not in chunk_files:
                            held_files_cur[rel_posix] = entry
                    current_cursor = {"files": held_files_cur}

                    cycle_has_errors = False
                    # A file whose chunk raised keeps its cursor at that chunk's start, so its
                    # later chunks wait for the next cycle
                    failed_files: set[str] = set()

                    for chunk in raw_read.chunks:
                        if chunk.rel_posix in failed_files:
                            continue
                        try:
                            # A chunk can be only an incomplete last record, with nothing to parse
                            outcome = (
                                await self._parsers.parse(
                                    project_id,
                                    s_id,
                                    chunk.content,
                                    origin_ref=chunk.rel_posix,
                                    start_offset=chunk.start_offset,
                                    csv_header=chunk.csv_header,
                                )
                                if chunk.content
                                else ParseOutcome(records=[], errors=[])
                            )
                        except Exception as exc:
                            cycle_has_errors = True
                            failed_files.add(chunk.rel_posix)
                            last_err = scrub_url_secrets(str(exc))
                            failures[s_id] = last_err
                            if self._incidents is not None:
                                try:
                                    _k, _ref, conn_name = parse_source_id(s_id)
                                    scope = conn_name if conn_name is not None else s_id
                                except ValueError:
                                    scope = s_id
                                is_suspended = await self._maintenance.suspended(
                                    project_id, scope, now
                                )
                                if not is_suspended:
                                    draft = SignalDraft(
                                        kind="parser_failure",
                                        category="operational",
                                        subject=s_id,
                                        ran_at=now,
                                        detail={"error": last_err, "source_id": s_id},
                                        source_ref=s_id,
                                    )
                                    await self._incidents.raise_signal(
                                        project_id, draft, seen_at=now
                                    )
                            continue

                        errors = list(outcome.errors)
                        if chunk.incomplete_tail_offset is not None:
                            errors.append(incomplete_record_error(chunk.incomplete_tail_offset))
                        if errors:
                            cycle_has_errors = True
                            for err in errors:
                                last_err = scrub_url_secrets(err)
                                failures[s_id] = last_err
                                if self._incidents is not None:
                                    try:
                                        _k, _ref, conn_name = parse_source_id(s_id)
                                        scope = conn_name if conn_name is not None else s_id
                                    except ValueError:
                                        scope = s_id
                                    is_suspended = await self._maintenance.suspended(
                                        project_id, scope, now
                                    )
                                    if not is_suspended:
                                        draft = SignalDraft(
                                            kind="parser_failure",
                                            category="operational",
                                            subject=s_id,
                                            ran_at=now,
                                            detail={"error": last_err, "source_id": s_id},
                                            source_ref=s_id,
                                        )
                                        await self._incidents.raise_signal(
                                            project_id, draft, seen_at=now
                                        )

                        if outcome.records:
                            stored = await self.ingest(
                                project_id, s_id, outcome.records, seen_at=now
                            )
                            all_newly_stored.extend(stored)
                            source_stored += len(stored)

                        # Advance cursor for successfully parsed chunk
                        files_cur = dict(current_cursor.get("files", {}))
                        files_cur[chunk.rel_posix] = {
                            "size": chunk.size,
                            "mtime": chunk.mtime,
                            "offset": chunk.new_offset,
                            "csv_header": chunk.csv_header,
                        }
                        current_cursor = {"files": files_cur}

                    if cycle_has_errors:
                        settings = await get_monitoring_settings(self._databases, project_id)
                        # One count per failed cycle, however many files failed in it:
                        # parser_disable_after counts cycles
                        if isinstance(self._parsers, SqliteParsers):
                            _new_f, newly_disabled = await self._parsers.record_failure(
                                project_id,
                                s_id,
                                cur_parser.version,
                                disable_after=settings.parser_disable_after,
                            )
                            if newly_disabled:
                                await self._events.record(
                                    project_id,
                                    source="results",
                                    type="parser.disabled",
                                    payload={
                                        "source_id": s_id,
                                        "version": cur_parser.version,
                                    },
                                )
                    elif raw_read.chunks and not failures.get(s_id):
                        if isinstance(self._parsers, SqliteParsers):
                            await self._parsers.reset_failures(project_id, s_id, cur_parser.version)

                consec_fail = (
                    0
                    if not failures.get(s_id)
                    else (state.consecutive_failures + 1 if state else 1)
                )
                await self._store.save_source_state(
                    project_id,
                    SourceState(
                        source_id=s_id,
                        kind=kind,
                        cursor=current_cursor,
                        last_read=now,
                        last_error=failures.get(s_id),
                        consecutive_failures=consec_fail,
                    ),
                )
                stored_total += source_stored
                by_source[s_id] = source_stored
                continue

            prev_failures = state.consecutive_failures if state else 0
            try:
                if kind == "file":
                    outcome = await self._file_reader.read(
                        project_id,
                        s_id,
                        cursor=current_cursor,
                        now=now,
                        consecutive_failures=prev_failures,
                    )
                elif kind == "ci":
                    if self._ci_reader is None:
                        raise RuntimeError("CI source reader is not configured")
                    outcome = await self._ci_reader.read(
                        project_id, s_id, cursor=current_cursor, consecutive_failures=prev_failures
                    )
                elif kind == "mlflow":
                    if self._mlflow_reader is None:
                        raise RuntimeError("MLflow source reader is not configured")
                    outcome = await self._mlflow_reader.read(
                        project_id, s_id, cursor=current_cursor, consecutive_failures=prev_failures
                    )
                else:
                    raise ValueError(f"Unknown source kind: {kind}")
            except Exception as exc:
                err_msg = scrub_url_secrets(str(exc))
                outcome = SourceReadOutcome(
                    records=[],
                    cursor=current_cursor,
                    consecutive_failures=prev_failures + 1,
                    last_error=err_msg,
                )

            # Store evaluation copies if any
            if outcome.evaluations:
                for eval_rec in outcome.evaluations:
                    try:
                        await self._evaluation_copies.store_copy(project_id, eval_rec)
                    except Exception:
                        logger.exception("Failed to store evaluation copy for %s", eval_rec)

            if outcome.last_error is not None:
                new_failures = (
                    outcome.consecutive_failures
                    if outcome.consecutive_failures > prev_failures
                    else prev_failures + 1
                )
            else:
                new_failures = 0

            new_state = SourceState(
                source_id=s_id,
                kind=kind,
                cursor=outcome.cursor,
                last_read=now,
                last_error=outcome.last_error,
                consecutive_failures=new_failures,
            )

            new_stored = await self._store.save_items_and_state(
                project_id,
                s_id,
                outcome.records,
                seen_at=now,
                state=new_state,
            )
            all_newly_stored.extend(new_stored)
            stored_total += len(new_stored)
            by_source[s_id] = len(new_stored)
            if outcome.last_error is not None:
                failures[s_id] = outcome.last_error
                # Only raise source_failure when the source starts failing (was not failing previously)
                was_failing = (
                    state is not None
                    and state.last_error is not None
                    and state.consecutive_failures > 0
                )
                if not was_failing and self._incidents is not None:
                    try:
                        _k, _ref, conn_name = parse_source_id(s_id)
                        scope = conn_name if conn_name is not None else s_id
                    except ValueError:
                        scope = s_id

                    is_suspended = await self._maintenance.suspended(project_id, scope, now)
                    if not is_suspended:
                        draft = SignalDraft(
                            kind="source_failure",
                            category="operational",
                            subject=s_id,
                            ran_at=now,
                            detail={"error": outcome.last_error, "source_id": s_id},
                            source_ref=s_id,
                        )
                        await self._incidents.raise_signal(project_id, draft, seen_at=now)

        # 4. Deliver all newly stored records sorted by ran_at ASC, id ASC
        if all_newly_stored and self._observers:
            all_newly_stored.sort(key=lambda r: (r.ran_at, r.id))
            for observer in self._observers:
                try:
                    await observer.stored(project_id, all_newly_stored)
                except Exception:
                    logger.exception(
                        "ResultsObserver %r raised during collection delivery for project %s",
                        observer,
                        project_id,
                    )
            await self._store.mark_delivered(
                project_id, [r.id for r in all_newly_stored], delivered_at=now
            )

        return CollectionReport(
            stored=stored_total,
            by_source=by_source,
            failures=failures,
        )

    async def ingest(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
    ) -> list[StoredResult]:
        """Ingests items into results table idempotently."""
        return await self._store.insert_items(project_id, source_id, items, seen_at)

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        """Queries stored results according to ResultQuery."""
        return await self._store.query(project_id, q)

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]:
        """Fetches newest stored result per check for subject and model."""
        return await self._store.latest_per_check(project_id, subject, model)
