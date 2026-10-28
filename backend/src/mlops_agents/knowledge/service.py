"""Knowledge service managing project notes, FTS search, hybrid search, and events (I17)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from sqlalchemy import delete, insert, select, text, update

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
from mlops_agents.knowledge.embedding import ReembedPass, embedder_key, embedding_input
from mlops_agents.knowledge.fusion import fuse
from mlops_agents.knowledge.limits import (
    EMBED_BATCH,
    MAX_QUERY_CHARS,
    SEARCH_CANDIDATES,
    SEARCH_MAX_LIMIT,
    TEXT_MAX_CHARS,
    TITLE_MAX_CHARS,
    VECTOR_CANDIDATES,
)
from mlops_agents.knowledge.notes import (
    get_note,
    get_notes_by_ids,
    list_newest_notes,
    query_fts_candidates,
    row_to_note_view,
)
from mlops_agents.knowledge.query import build_fts_query
from mlops_agents.knowledge.state import get_vectors_ready_for, set_vectors_ready_for
from mlops_agents.knowledge.vectors import VectorStoreError
from mlops_agents.providers.embedder import Embedders, EmbedderUnavailable
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import note_embeddings, notes


class KnowledgeService(Knowledge):
    """Knowledge service handling notes CRUD, full-text and vector search, and events."""

    def __init__(
        self,
        databases: Databases,
        embedders: Embedders,
        vectors: VectorStore,
        events: EventLog,
        secrets: Callable[[], Awaitable[Sequence[str]]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._databases = databases
        self._embedders = embedders
        self._vectors = vectors
        self._events = events
        self._secrets = secrets
        self._monotonic = monotonic
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._passes: dict[str, ReembedPass] = {}

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
        text_content = scrub_values(scrub_url_secrets(note.text), secret_values)
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
                with contextlib.suppress(VectorStoreError):
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
                            text=text_content,
                            source=source,
                            updated_at=now_ts,
                        )
                    )

                view = NoteView(
                    id=note_id,
                    type=note.type,
                    title=title,
                    text=text_content,
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
                self.kick(project_id)
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
                        text=text_content,
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
                text=text_content,
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
            self.kick(project_id)
            return view

    async def delete(self, project_id: str, note_id: str) -> None:
        lock = self._get_project_lock(project_id)
        async with lock:
            with contextlib.suppress(VectorStoreError):
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

        # 1. Full text candidates computed first. If query has no tokens, return newest notes.
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

        # 2. Check embedder for project. If None: full-text only.
        embedder = await self._embedders.for_project(project_id)
        if embedder is None:
            ranked_views = await get_notes_by_ids(
                engine,
                candidate_ids[:clamped_limit],
                note_type=type,
            )
            st = await self._embedders.status(project_id)
            detail = (
                st.problem
                or "No embedder is set, so search matches words only. Choose one in the AI providers settings."
            )
            return SearchResult(notes=ranked_views, used_vectors=False, detail=detail)

        # 3. Check ready setting. If not ready for current embedder key: kick and return full text with detail.
        key = embedder_key(embedder.address, embedder.dimensions)
        ready_key = await get_vectors_ready_for(self._databases, project_id)
        if ready_key != key:
            self.kick(project_id)
            ranked_views = await get_notes_by_ids(
                engine,
                candidate_ids[:clamped_limit],
                note_type=type,
            )
            pass_state = self._passes.get(project_id)
            if (
                pass_state is not None
                and not pass_state.should_retry(self._monotonic())
                and pass_state.last_failure_msg is not None
            ):
                detail = pass_state.last_failure_msg
            else:
                detail = f"Notes are being re-embedded with {embedder.address}; until that finishes, search matches words only."
            return SearchResult(notes=ranked_views, used_vectors=False, detail=detail)

        # 4. Embedder ready and matches current key.
        pass_state = self._passes.get(project_id)
        if pass_state is not None and not pass_state.should_retry(self._monotonic()):
            ranked_views = await get_notes_by_ids(
                engine,
                candidate_ids[:clamped_limit],
                note_type=type,
            )
            return SearchResult(
                notes=ranked_views,
                used_vectors=False,
                detail=pass_state.last_failure_msg,
            )

        try:
            vecs = await embedder.embed([query[:MAX_QUERY_CHARS]])
            query_vec = vecs[0]
            vector_ids = await self._vectors.nearest(
                project_id, query_vec, key, k=VECTOR_CANDIDATES
            )
        except (EmbedderUnavailable, VectorStoreError) as exc:
            if pass_state is None:
                pass_state = self._passes.setdefault(project_id, ReembedPass())
            pass_state.record_failure(str(exc), self._monotonic())
            ranked_views = await get_notes_by_ids(
                engine,
                candidate_ids[:clamped_limit],
                note_type=type,
            )
            return SearchResult(notes=ranked_views, used_vectors=False, detail=str(exc))

        # Filter vector candidates by type if specified
        if type is not None and vector_ids:
            type_matched_views = await get_notes_by_ids(engine, vector_ids, note_type=type)
            filtered_vector_ids = [v.id for v in type_matched_views]
        else:
            filtered_vector_ids = vector_ids

        # Combine rankings using Reciprocal Rank Fusion
        fused_ids = fuse([candidate_ids, filtered_vector_ids])
        ranked_views = await get_notes_by_ids(
            engine,
            fused_ids[:clamped_limit],
            note_type=type,
        )
        return SearchResult(notes=ranked_views, used_vectors=True, detail=None)

    def kick(self, project_id: str) -> None:
        """Kick re-embedding pass for project, honoring cooldown and single-flight."""
        pass_state = self._passes.setdefault(project_id, ReembedPass())
        now = self._monotonic()
        if not pass_state.should_retry(now):
            return
        if pass_state.running:
            pass_state.rerun = True
            return
        pass_state.task = asyncio.create_task(self._run_pass(project_id, pass_state))

    async def _run_pass(self, project_id: str, pass_state: ReembedPass) -> None:
        try:
            while True:
                embedder = await self._embedders.for_project(project_id)
                if embedder is None:
                    return

                key = embedder_key(embedder.address, embedder.dimensions)
                engine = await self._databases.project(project_id)

                while True:
                    stmt = (
                        select(notes)
                        .outerjoin(
                            note_embeddings,
                            (note_embeddings.c.note_id == notes.c.id)
                            & (note_embeddings.c.embedder == key),
                        )
                        .where(note_embeddings.c.note_id.is_(None))
                        .order_by(notes.c.created_at.asc(), notes.c.id.asc())
                        .limit(EMBED_BATCH)
                    )
                    async with engine.connect() as conn:
                        res = await conn.execute(stmt)
                        rows = res.mappings().all()

                    if not rows:
                        break

                    batch = [row_to_note_view(row) for row in rows]
                    inputs = [embedding_input(n) for n in batch]

                    vecs = await embedder.embed(inputs)

                    for note, vec, inp in zip(batch, vecs, inputs, strict=True):
                        async with self._get_project_lock(project_id):
                            current_note = await get_note(engine, note.id)
                            if current_note is None or embedding_input(current_note) != inp:
                                continue
                            await self._vectors.upsert(project_id, note.id, vec, key)

                if pass_state.rerun:
                    pass_state.rerun = False
                    continue

                # Clean up old embedder tables and rows
                old_keys: set[str] = set()
                ready_key = await get_vectors_ready_for(self._databases, project_id)
                if ready_key and ready_key != key:
                    old_keys.add(ready_key)

                async with engine.connect() as conn:
                    emb_res = await conn.execute(
                        select(note_embeddings.c.embedder)
                        .where(note_embeddings.c.embedder != key)
                        .distinct()
                    )
                    for r in emb_res.fetchall():
                        old_keys.add(str(r[0]))

                    tbl_res = await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'note_vec_%'"
                        )
                    )
                    for row in tbl_res.fetchall():
                        tbl_name = str(row[0])
                        if (
                            tbl_name.startswith("note_vec_")
                            and tbl_name[len("note_vec_") :].isdigit()
                        ):
                            vec_res = await conn.execute(
                                text(
                                    f"SELECT DISTINCT embedder FROM {tbl_name} WHERE embedder != :key"  # noqa: S608
                                ),
                                {"key": key},
                            )
                            for vr in vec_res.fetchall():
                                if vr[0]:
                                    old_keys.add(str(vr[0]))

                for old_key in old_keys:
                    await self._vectors.drop_embedder(project_id, old_key)

                await set_vectors_ready_for(self._databases, project_id, key)
                pass_state.clear_failure()
                if pass_state.rerun:
                    pass_state.rerun = False
                    continue
                break

        except asyncio.CancelledError:
            raise
        except (EmbedderUnavailable, VectorStoreError) as exc:
            pass_state.record_failure(str(exc), self._monotonic())
        except Exception as exc:
            pass_state.record_failure(str(exc), self._monotonic())
        finally:
            pass_state.task = None

    async def wait_idle(self, project_id: str) -> None:
        """Await running re-embedding pass for project."""
        while True:
            pass_state = self._passes.get(project_id)
            if pass_state is None or pass_state.task is None or pass_state.task.done():
                break
            try:
                await pass_state.task
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def close(self) -> None:
        """Cancel and await all running background re-embedding passes."""
        tasks: list[asyncio.Task[None]] = []
        for pass_state in self._passes.values():
            if pass_state.task is not None and not pass_state.task.done():
                pass_state.task.cancel()
                tasks.append(pass_state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
