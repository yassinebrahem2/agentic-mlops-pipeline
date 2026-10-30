"""ResultsService implementation and collection loop (I22)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from mlops_agents.connections.contract import Connections
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.contract import (
    CollectionReport,
    ResultQuery,
    ResultRecord,
    Results,
    ResultsObserver,
    SourceKind,
    SourceState,
    StoredResult,
    source_id,
)
from mlops_agents.monitoring.results.file_source import FileSourceReader, SourceReadOutcome
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import FileResults, JobFeedRef
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page

logger = logging.getLogger(__name__)


class SourceReader(Protocol):
    """Protocol for reading a source kind."""

    async def read(
        self, project_id: str, source_id: str, cursor: dict[str, object]
    ) -> SourceReadOutcome: ...


class ResultsService(Results):
    """Main results service managing collection, storage, delivery and queries."""

    def __init__(
        self,
        databases: Databases,
        handoffs: Handoffs,
        connections: Connections,
        events: SqliteEventLog,
        *,
        store: ResultsStore | None = None,
        file_reader: FileSourceReader | None = None,
    ) -> None:
        self._databases = databases
        self._handoffs = handoffs
        self._connections = connections
        self._events = events
        self._store = store or ResultsStore(databases)
        self._file_reader = file_reader or FileSourceReader(connections)
        self._observers: list[ResultsObserver] = []

    @property
    def store(self) -> ResultsStore:
        return self._store

    def add_observer(self, observer: ResultsObserver) -> None:
        self._observers.append(observer)

    async def deliver_undelivered(self, project_id: str, now: datetime) -> None:
        """Delivers any previously committed but undelivered results to observers."""
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
                derived[s_id] = ("file", False)

            # Standard check specs
            for check in ops.checks:
                if isinstance(check.results, FileResults):
                    s_id = source_id(
                        "file",
                        check.results.path_pattern,
                        check.results.connection,
                    )
                    derived[s_id] = ("file", False)

            # Team job feeds (needs a parser)
            for feed in ops.feeds:
                if isinstance(feed.ref, JobFeedRef) and feed.ref.file is not None:
                    s_id = source_id(
                        "file",
                        feed.ref.file.path_pattern,
                        feed.ref.file.connection,
                    )
                    derived[s_id] = ("file", True)

        return [(s_id, kind, needs_parser) for s_id, (kind, needs_parser) in derived.items()]

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        """Runs a collection cycle for all derived sources."""
        # 1. Deliver any undelivered records first
        await self.deliver_undelivered(project_id, now)

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
                # In Task 2, team sources that need a parser have none yet: do not read, cursor stays at 0
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

            outcome = await self._file_reader.read(project_id, s_id, cursor=current_cursor)
            new_state = SourceState(
                source_id=s_id,
                kind=kind,
                cursor=outcome.cursor,
                last_read=now,
                last_error=outcome.last_error,
                consecutive_failures=outcome.consecutive_failures,
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

        # 4. Deliver all newly stored records sorted by ran_at ASC, id ASC
        if all_newly_stored:
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

        report = CollectionReport(
            stored=stored_total,
            by_source=by_source,
            failures=failures,
        )

        # 5. Emit results.collected event
        await self._events.record(
            project_id,
            source="results",
            type="results.collected",
            payload=report.model_dump(),
        )

        return report

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
