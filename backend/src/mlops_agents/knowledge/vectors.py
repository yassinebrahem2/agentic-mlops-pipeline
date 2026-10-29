"""Sqlite-vec implementation of VectorStore (I17)."""

from __future__ import annotations

import logging
import math
import struct
from collections.abc import Sequence
from weakref import WeakSet

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.clock import now_iso
from mlops_agents.knowledge.limits import MAX_DIMENSIONS
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import note_embeddings, notes

logger = logging.getLogger(__name__)

FLOAT32_MAX = 3.4028234663852886e38


class VectorStoreError(Exception):
    """User-facing error raised when vector search fails or is unavailable."""


def _validate_vector(vector: Sequence[float]) -> int:
    dim = len(vector)
    if dim < 1 or dim > MAX_DIMENSIONS:
        raise VectorStoreError(
            f"Vector dimensions must be between 1 and {MAX_DIMENSIONS}, got {dim}"
        )
    for v in vector:
        if not math.isfinite(v):
            raise VectorStoreError("Vector contains non-finite values (NaN or Inf)")
        if abs(v) > FLOAT32_MAX:
            raise VectorStoreError("Vector component exceeds 32-bit float range")
    return dim


class SqliteVectorStore:
    """Vector store backed by per-dimension vec0 virtual tables in SQLite."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases
        self._verified_engines: WeakSet[AsyncEngine] = WeakSet()

    async def _ensure_vec_available(self, engine: AsyncEngine) -> None:
        if engine in self._verified_engines:
            return
        try:
            async with engine.connect() as conn:
                res = await conn.execute(text("SELECT vec_version()"))
                version = res.scalar()
                if not version:
                    raise VectorStoreError("Vector search is not available on this installation.")
        except Exception as exc:
            logger.warning("Vector extension check failed: %s", exc)
            raise VectorStoreError("Vector search is not available on this installation.") from exc
        self._verified_engines.add(engine)

    async def upsert(
        self, project_id: str, note_id: str, vector: list[float], embedder: str
    ) -> None:
        dim = _validate_vector(vector)
        engine = await self._databases.project(project_id)
        await self._ensure_vec_available(engine)

        blob = struct.pack(f"{dim}f", *vector)
        now_ts = now_iso()

        async with engine.begin() as conn:
            # Map note_id to seq
            seq_res = await conn.execute(select(notes.c.seq).where(notes.c.id == note_id))
            seq = seq_res.scalar()
            if seq is None:
                raise VectorStoreError(f"Note not found: {note_id}")

            # Check existing note_embeddings row and delete from old table if dimension changed
            emb_res = await conn.execute(
                select(note_embeddings.c.dimensions).where(note_embeddings.c.note_id == note_id)
            )
            old_dim = emb_res.scalar()
            if old_dim is not None and old_dim != dim:
                old_table = f"note_vec_{old_dim}"
                tbl_check = await conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                    {"name": old_table},
                )
                if tbl_check.scalar() is not None:
                    await conn.execute(
                        text(f"DELETE FROM {old_table} WHERE rowid = :seq"),  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                        {"seq": seq},
                    )

            table_name = f"note_vec_{dim}"
            await conn.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0("
                    f"embedding FLOAT[{dim}] DISTANCE_METRIC=cosine, embedder TEXT)"
                )
            )

            # Delete old vector by rowid = seq before inserting so a stray row never fails
            await conn.execute(
                text(f"DELETE FROM {table_name} WHERE rowid = :seq"),  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                {"seq": seq},
            )

            # Insert new vector
            await conn.execute(
                text(
                    f"INSERT INTO {table_name}(rowid, embedding, embedder) "  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                    "VALUES (:seq, :vec, :embedder)"
                ),
                {"seq": seq, "vec": blob, "embedder": embedder},
            )

            # Replace note_embeddings row
            await conn.execute(delete(note_embeddings).where(note_embeddings.c.note_id == note_id))
            await conn.execute(
                insert(note_embeddings).values(
                    note_id=note_id,
                    embedder=embedder,
                    dimensions=dim,
                    embedded_at=now_ts,
                )
            )

    async def nearest(
        self, project_id: str, vector: list[float], embedder: str, k: int
    ) -> list[str]:
        dim = _validate_vector(vector)
        if k <= 0:
            return []

        engine = await self._databases.project(project_id)
        await self._ensure_vec_available(engine)

        table_name = f"note_vec_{dim}"
        blob = struct.pack(f"{dim}f", *vector)

        try:
            async with engine.connect() as conn:
                tbl_check = await conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                    {"name": table_name},
                )
                if tbl_check.scalar() is None:
                    return []

                stmt = text(
                    f"SELECT rowid FROM {table_name} "  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                    "WHERE embedding MATCH :q AND k = :k AND embedder = :e "
                    "ORDER BY distance"
                )
                res = await conn.execute(stmt, {"q": blob, "k": k, "e": embedder})
                rowids = [row[0] for row in res.fetchall()]
                if not rowids:
                    return []

                notes_res = await conn.execute(
                    select(notes.c.seq, notes.c.id).where(notes.c.seq.in_(rowids))
                )
                seq_to_id = {seq: nid for seq, nid in notes_res.fetchall()}
                return [seq_to_id[seq] for seq in rowids if seq in seq_to_id]
        except VectorStoreError:
            raise
        except Exception as exc:
            logger.warning("Vector search query execution failed: %s", exc)
            raise VectorStoreError("Vector search query failed.") from exc

    async def remove(self, project_id: str, note_id: str) -> None:
        engine = await self._databases.project(project_id)

        # Step 1: Always delete and commit note_embeddings in its own transaction first.
        # This must be unconditional even if vec0 extension is unavailable or errors.
        async with engine.begin() as conn:
            seq_res = await conn.execute(select(notes.c.seq).where(notes.c.id == note_id))
            seq = seq_res.scalar()

            emb_res = await conn.execute(
                select(note_embeddings.c.dimensions).where(note_embeddings.c.note_id == note_id)
            )
            dim = emb_res.scalar()

            await conn.execute(delete(note_embeddings).where(note_embeddings.c.note_id == note_id))

        # Step 2: Remove the corresponding vec0 row if a vec table exists.
        if seq is not None and dim is not None:
            table_name = f"note_vec_{dim}"
            async with engine.connect() as conn:
                tbl_check = await conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                    {"name": table_name},
                )
                tbl_exists = tbl_check.scalar() is not None

            if tbl_exists:
                try:
                    await self._ensure_vec_available(engine)
                    async with engine.begin() as conn:
                        await conn.execute(
                            text(f"DELETE FROM {table_name} WHERE rowid = :seq"),  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                            {"seq": seq},
                        )
                        count_res = await conn.execute(
                            text(f"SELECT count(*) FROM {table_name}")  # noqa: S608 -- table name is dynamic per-dimension int validated against 1..MAX_DIMENSIONS
                        )
                        if count_res.scalar() == 0:
                            await conn.execute(text(f"DROP TABLE {table_name}"))
                except VectorStoreError as exc:
                    logger.warning(
                        "Vector extension unavailable while removing vec0 row for note %s: %s",
                        note_id,
                        exc,
                    )
                    raise

    async def drop_embedder(self, project_id: str, embedder: str) -> None:
        engine = await self._databases.project(project_id)
        await self._ensure_vec_available(engine)

        async with engine.begin() as conn:
            tables_res = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'note_vec_%'"
                )
            )
            prefix = "note_vec_"
            for row in tables_res.fetchall():
                tbl_name = str(row[0])
                if tbl_name.startswith(prefix) and tbl_name[len(prefix) :].isdigit():
                    dim = int(tbl_name[len(prefix) :])
                    if 1 <= dim <= MAX_DIMENSIONS:
                        await conn.execute(
                            text(f"DELETE FROM {tbl_name} WHERE embedder = :embedder"),  # noqa: S608 -- table name is verified to be note_vec_{dim} with 1..MAX_DIMENSIONS
                            {"embedder": embedder},
                        )
                        count_res = await conn.execute(
                            text(f"SELECT count(*) FROM {tbl_name}")  # noqa: S608 -- table name is verified to be note_vec_{dim} with 1..MAX_DIMENSIONS
                        )
                        if count_res.scalar() == 0:
                            await conn.execute(text(f"DROP TABLE {tbl_name}"))

            await conn.execute(
                delete(note_embeddings).where(note_embeddings.c.embedder == embedder)
            )
