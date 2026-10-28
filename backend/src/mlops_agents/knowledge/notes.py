"""Database mapping and queries for project knowledge notes (I17)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import RowMapping, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.knowledge.contract import NoteType, NoteView
from mlops_agents.storage.tables import notes


def row_to_note_view(row: RowMapping | Mapping[str, Any]) -> NoteView:
    """Map a notes table row mapping to NoteView, parsing ISO timestamps."""
    return NoteView(
        id=str(row["id"]),
        type=row["type"],
        title=str(row["title"]),
        text=str(row["text"]),
        source=str(row["source"]) if row["source"] is not None else None,
        author=str(row["author"]),
        created=datetime.fromisoformat(str(row["created_at"])),
        updated=datetime.fromisoformat(str(row["updated_at"])),
    )


async def get_note(engine: AsyncEngine, note_id: str) -> NoteView | None:
    """Read a single note by id, or None if not found."""
    async with engine.connect() as conn:
        res = await conn.execute(select(notes).where(notes.c.id == note_id))
        row = res.mappings().first()
        return row_to_note_view(row) if row is not None else None


async def get_notes_by_ids(
    engine: AsyncEngine,
    note_ids: Sequence[str],
    *,
    note_type: NoteType | None = None,
) -> list[NoteView]:
    """Fetch NoteViews for a list of note IDs in matching order, dropping missing IDs.

    Applies an optional note_type filter. Duplicate IDs in note_ids are deduplicated
    in result.
    """
    if not note_ids:
        return []

    stmt = select(notes).where(notes.c.id.in_(note_ids))
    if note_type is not None:
        stmt = stmt.where(notes.c.type == note_type)

    async with engine.connect() as conn:
        res = await conn.execute(stmt)
        rows = res.mappings().all()

    id_to_view = {str(row["id"]): row_to_note_view(row) for row in rows}
    result: list[NoteView] = []
    seen: set[str] = set()
    for nid in note_ids:
        if nid in id_to_view and nid not in seen:
            seen.add(nid)
            result.append(id_to_view[nid])
    return result


async def list_newest_notes(
    engine: AsyncEngine,
    *,
    limit: int,
    note_type: NoteType | None = None,
) -> list[NoteView]:
    """Return the newest notes ordered by updated_at descending, optionally filtered by type."""
    if limit <= 0:
        return []

    stmt = select(notes)
    if note_type is not None:
        stmt = stmt.where(notes.c.type == note_type)
    stmt = stmt.order_by(notes.c.updated_at.desc(), notes.c.id.desc()).limit(limit)

    async with engine.connect() as conn:
        res = await conn.execute(stmt)
        rows = res.mappings().all()

    return [row_to_note_view(row) for row in rows]


async def query_fts_candidates(
    engine: AsyncEngine,
    match_query: str,
    *,
    limit: int,
    note_type: NoteType | None = None,
) -> list[str]:
    """Query FTS candidate IDs ordered by bm25(title 5.0, text 1.0).

    Returns up to limit candidate note IDs.
    """
    if limit <= 0 or not match_query:
        return []

    if note_type is not None:
        sql = text(
            "SELECT n.id FROM notes_fts "
            "JOIN notes n ON n.seq = notes_fts.rowid "
            "WHERE notes_fts MATCH :q AND n.type = :type "
            "ORDER BY bm25(notes_fts, 5.0, 1.0) "
            "LIMIT :limit"
        )
        params = {"q": match_query, "type": note_type, "limit": limit}
    else:
        sql = text(
            "SELECT n.id FROM notes_fts "
            "JOIN notes n ON n.seq = notes_fts.rowid "
            "WHERE notes_fts MATCH :q "
            "ORDER BY bm25(notes_fts, 5.0, 1.0) "
            "LIMIT :limit"
        )
        params = {"q": match_query, "limit": limit}

    async with engine.connect() as conn:
        res = await conn.execute(sql, params)
        return [str(row[0]) for row in res.fetchall()]
