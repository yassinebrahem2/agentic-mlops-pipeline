"""Knowledge service managing project notes, FTS search, and events (I17)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from sqlalchemy import delete, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.events.log import EventLog
from mlops_agents.ids import new_id
from mlops_agents.knowledge.contract import (
    Knowledge,
    NoteDraft,
    NoteType,
    NoteView,
    SearchResult,
    VectorStore,
)
from mlops_agents.knowledge.limits import (
    SEARCH_CANDIDATES,
    SEARCH_MAX_LIMIT,
    TEXT_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from mlops_agents.knowledge.notes import (
    get_note,
    get_notes_by_ids,
    list_newest_notes,
    query_fts_candidates,
)
from mlops_agents.knowledge.query import build_fts_query
from mlops_agents.providers.embedder import Embedders
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notes


class KnowledgeService(Knowledge):
    """Knowledge service handling notes CRUD, full-text search, and events."""

    def __init__(
        self,
        databases: Databases,
        embedders: Embedders,
        vectors: VectorStore,
        events: EventLog,
        secrets: Callable[[], Awaitable[Sequence[str]]],
    ) -> None:
        self._databases = databases
        self._embedders = embedders
        self._vectors = vectors
        self._events = events
        self._secrets = secrets
        self._project_locks: dict[str, asyncio.Lock] = {}

    def _get_project_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

    async def save(
        self,
        project_id: str,
        author: str,
        note: NoteDraft,
        note_id: str | None = None,
    ) -> NoteView:
        # Validate lengths
        if len(note.title) > TITLE_MAX_CHARS:
            raise DomainError(
                "note_too_long",
                f"Title exceeds maximum length of {TITLE_MAX_CHARS} characters",
                status=422,
            )
        if len(note.text) > TEXT_MAX_CHARS:
            raise DomainError(
                "note_too_long",
                f"Text exceeds maximum length of {TEXT_MAX_CHARS} characters",
                status=422,
            )

        # Validate non-empty
        if not note.title.strip() or not note.text.strip():
            raise DomainError(
                "invalid_note",
                "Title and text cannot be empty",
                status=422,
            )

        # Scrub secrets
        secret_values = await self._secrets()
        title = scrub_values(scrub_url_secrets(note.title), secret_values)
        text = scrub_values(scrub_url_secrets(note.text), secret_values)
        source = (
            scrub_values(scrub_url_secrets(note.source), secret_values)
            if note.source is not None
            else None
        )

        lock = self._get_project_lock(project_id)
        async with lock:
            engine = await self._databases.project(project_id)

            if note_id is not None:
                # Update existing note: remove old vector first
                await self._vectors.remove(project_id, note_id)

                async with engine.begin() as conn:
                    existing_res = await conn.execute(
                        select(notes.c.id, notes.c.author, notes.c.created_at).where(
                            notes.c.id == note_id
                        )
                    )
                    existing = existing_res.mappings().first()
                    if existing is None:
                        raise DomainError(
                            "note_not_found",
                            f"Note {note_id} not found",
                            status=404,
                        )

                    now_ts = now_iso()
                    await conn.execute(
                        update(notes)
                        .where(notes.c.id == note_id)
                        .values(
                            type=note.type,
                            title=title,
                            text=text,
                            source=source,
                            updated_at=now_ts,
                        )
                    )

                view = NoteView(
                    id=note_id,
                    type=note.type,
                    title=title,
                    text=text,
                    source=source,
                    author=str(existing["author"]),
                    created=datetime.fromisoformat(str(existing["created_at"])),
                    updated=datetime.fromisoformat(now_ts),
                )

                await self._events.record(
                    project_id,
                    source=author,
                    type="note.saved",
                    subject_id=note_id,
                    payload={"note_id": note_id, "type": note.type, "title": title},
                )
                return view

            # Create new note
            nid = new_id("kno")
            now_ts = now_iso()

            async with engine.begin() as conn:
                await conn.execute(
                    insert(notes).values(
                        id=nid,
                        type=note.type,
                        title=title,
                        text=text,
                        source=source,
                        author=author,
                        created_at=now_ts,
                        updated_at=now_ts,
                    )
                )

            now_dt = datetime.fromisoformat(now_ts)
            view = NoteView(
                id=nid,
                type=note.type,
                title=title,
                text=text,
                source=source,
                author=author,
                created=now_dt,
                updated=now_dt,
            )

            await self._events.record(
                project_id,
                source=author,
                type="note.saved",
                subject_id=nid,
                payload={"note_id": nid, "type": note.type, "title": title},
            )
            return view

    async def delete(self, project_id: str, note_id: str) -> None:
        lock = self._get_project_lock(project_id)
        async with lock:
            await self._vectors.remove(project_id, note_id)

            engine = await self._databases.project(project_id)
            async with engine.begin() as conn:
                res = await conn.execute(select(notes.c.type).where(notes.c.id == note_id))
                existing_type = res.scalar()
                if existing_type is None:
                    raise DomainError(
                        "note_not_found",
                        f"Note {note_id} not found",
                        status=404,
                    )

                await conn.execute(delete(notes).where(notes.c.id == note_id))

            await self._events.record(
                project_id,
                source="user",
                type="note.deleted",
                subject_id=note_id,
                payload={"note_id": note_id, "type": existing_type},
            )

    async def get(self, project_id: str, note_id: str) -> NoteView | None:
        engine = await self._databases.project(project_id)
        return await get_note(engine, note_id)

    async def search(
        self,
        project_id: str,
        query: str,
        type: NoteType | None,
        limit: int,
    ) -> SearchResult:
        clamped_limit = max(1, min(limit, SEARCH_MAX_LIMIT))
        fts_query = build_fts_query(query)

        engine = await self._databases.project(project_id)

        if fts_query is None:
            newest = await list_newest_notes(
                engine,
                limit=clamped_limit,
                note_type=type,
            )
            return SearchResult(notes=newest, used_vectors=False, detail=None)

        candidate_ids = await query_fts_candidates(
            engine,
            fts_query,
            limit=SEARCH_CANDIDATES,
            note_type=type,
        )
        ranked_views = await get_notes_by_ids(
            engine,
            candidate_ids[:clamped_limit],
            note_type=type,
        )

        embedder = await self._embedders.for_project(project_id)
        detail: str | None = None
        if embedder is None:
            st = await self._embedders.status(project_id)
            detail = (
                st.problem
                or "No embedder is set, so search matches words only. Choose one in the AI providers settings."
            )

        return SearchResult(notes=ranked_views, used_vectors=False, detail=detail)

    async def close(self) -> None:
        """Close running background passes (extended in task 3)."""
        pass
