"""Embedding formatting, keys, and re-embedding state (I17)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from mlops_agents.credentials.scrub import error_text
from mlops_agents.knowledge.contract import NoteDraft, NoteView, VectorStore
from mlops_agents.knowledge.limits import EMBED_BATCH, EMBED_INPUT_CHARS, RETRY_AFTER_SECONDS
from mlops_agents.knowledge.notes import get_note, row_to_note_view
from mlops_agents.knowledge.state import get_vectors_ready_for, set_vectors_ready_for
from mlops_agents.knowledge.vectors import VectorStoreError
from mlops_agents.providers.embedder import Embedders, EmbedderUnavailable
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import note_embeddings, notes

logger = logging.getLogger(__name__)


def embedder_key(address: str, dimensions: int) -> str:
    """Return embedder key combining address and dimensions."""
    return f"{address}@{dimensions}"


def embedding_input(note: NoteDraft | NoteView) -> str:
    """Format a note's title and text for embedding input, clamped to EMBED_INPUT_CHARS."""
    return f"{note.title}\n\n{note.text}"[:EMBED_INPUT_CHARS]


@dataclass
class ReembedPass:
    """Per-project state for background re-embedding passes."""

    task: asyncio.Task[None] | None = None
    rerun: bool = False
    last_failure_time: float | None = None
    last_failure_msg: str | None = None

    @property
    def running(self) -> bool:
        """True if the background task is running and not done."""
        return self.task is not None and not self.task.done()

    def should_retry(self, now: float, retry_after: float = RETRY_AFTER_SECONDS) -> bool:
        """True if no failure has been recorded or cooldown period has elapsed."""
        if self.last_failure_time is None:
            return True
        return (now - self.last_failure_time) >= retry_after

    def record_failure(self, message: str, now: float) -> None:
        """Record an embedder or vector store failure timestamp and message."""
        self.last_failure_msg = message
        self.last_failure_time = now

    def clear_failure(self) -> None:
        """Clear recorded failure state on recovery."""
        self.last_failure_msg = None
        self.last_failure_time = None


async def run_reembed_pass(
    project_id: str,
    pass_state: ReembedPass,
    *,
    databases: Databases,
    embedders: Embedders,
    vectors: VectorStore,
    get_project_lock: Callable[[str], asyncio.Lock],
    monotonic: Callable[[], float],
) -> None:
    """Run a single-flight background re-embedding pass for a project."""
    try:
        while True:
            embedder = await embedders.for_project(project_id)
            if embedder is None:
                return

            key = embedder_key(embedder.address, embedder.dimensions)
            engine = await databases.project(project_id)

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
                    async with get_project_lock(project_id):
                        current_note = await get_note(engine, note.id)
                        if current_note is None or embedding_input(current_note) != inp:
                            continue
                        await vectors.upsert(project_id, note.id, vec, key)

            if pass_state.rerun:
                pass_state.rerun = False
                continue

            # Clean up old embedders
            async with engine.connect() as conn:
                emb_res = await conn.execute(
                    select(note_embeddings.c.embedder)
                    .where(note_embeddings.c.embedder != key)
                    .distinct()
                )
                old_keys = [str(r[0]) for r in emb_res.fetchall() if r[0]]

            ready_key = await get_vectors_ready_for(databases, project_id)
            if ready_key and ready_key != key and ready_key not in old_keys:
                old_keys.append(ready_key)

            for old_key in old_keys:
                await vectors.drop_embedder(project_id, old_key)

            await set_vectors_ready_for(databases, project_id, key)
            pass_state.clear_failure()
            if pass_state.rerun:
                pass_state.rerun = False
                continue
            break

    except asyncio.CancelledError:
        raise
    except (EmbedderUnavailable, VectorStoreError) as exc:
        logger.warning("Re-embedding pass failed: %s", exc)
        pass_state.record_failure(error_text(exc), monotonic())
    except Exception as exc:
        logger.exception("Unexpected error in re-embedding pass: %s", exc)
        pass_state.record_failure(error_text(exc), monotonic())
    finally:
        pass_state.task = None
