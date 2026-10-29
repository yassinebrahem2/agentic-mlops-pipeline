from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, insert, text, update

from mlops_agents.knowledge.notes import (
    get_note,
    get_notes_by_ids,
    list_newest_notes,
    query_fts_candidates,
    row_to_note_view,
)
from mlops_agents.knowledge.query import build_fts_query
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import create_engine_for
from mlops_agents.storage.tables import notes


async def test_row_to_note_view_parses_timestamps() -> None:
    row = {
        "id": "kno_123",
        "type": "fact",
        "title": "A fact title",
        "text": "Fact text content",
        "source": "https://example.com",
        "author": "tester",
        "created_at": "2026-10-28T10:00:00.000Z",
        "updated_at": "2026-10-28T11:00:00.000Z",
    }
    view = row_to_note_view(row)
    assert view.id == "kno_123"
    assert view.type == "fact"
    assert view.title == "A fact title"
    assert view.text == "Fact text content"
    assert view.source == "https://example.com"
    assert view.author == "tester"
    assert view.created.isoformat().startswith("2026-10-28T10:00:00")
    assert view.updated.isoformat().startswith("2026-10-28T11:00:00")


async def test_get_notes_by_ids_preserves_order_and_drops_missing(databases: Databases) -> None:
    project_id = "prj_notesids"
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                [
                    {
                        "id": "kno_1",
                        "type": "fact",
                        "title": "First",
                        "text": "Content 1",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                    {
                        "id": "kno_2",
                        "type": "decision",
                        "title": "Second",
                        "text": "Content 2",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:01:00Z",
                        "updated_at": "2026-10-28T10:01:00Z",
                    },
                    {
                        "id": "kno_3",
                        "type": "fact",
                        "title": "Third",
                        "text": "Content 3",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:02:00Z",
                        "updated_at": "2026-10-28T10:02:00Z",
                    },
                ]
            )
        )

    # Missing ID kno_missing dropped; order kno_3, kno_1 preserved
    views = await get_notes_by_ids(engine, ["kno_3", "kno_missing", "kno_1"])
    assert [v.id for v in views] == ["kno_3", "kno_1"]

    # Filter by note_type
    filtered = await get_notes_by_ids(engine, ["kno_1", "kno_2", "kno_3"], note_type="decision")
    assert [v.id for v in filtered] == ["kno_2"]

    # Empty IDs
    assert await get_notes_by_ids(engine, []) == []

    # Single get_note
    single = await get_note(engine, "kno_2")
    assert single is not None
    assert single.title == "Second"

    missing = await get_note(engine, "kno_nonexistent")
    assert missing is None


async def test_list_newest_notes_ordering_and_filters(databases: Databases) -> None:
    project_id = "prj_notesnewest"
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                [
                    {
                        "id": "kno_old",
                        "type": "fact",
                        "title": "Old",
                        "text": "...",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T09:00:00Z",
                        "updated_at": "2026-10-28T09:00:00Z",
                    },
                    {
                        "id": "kno_mid",
                        "type": "incident",
                        "title": "Mid",
                        "text": "...",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                    {
                        "id": "kno_new",
                        "type": "fact",
                        "title": "New",
                        "text": "...",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T11:00:00Z",
                        "updated_at": "2026-10-28T11:00:00Z",
                    },
                ]
            )
        )

    all_notes = await list_newest_notes(engine, limit=10)
    assert [n.id for n in all_notes] == ["kno_new", "kno_mid", "kno_old"]

    clamped = await list_newest_notes(engine, limit=2)
    assert [n.id for n in clamped] == ["kno_new", "kno_mid"]

    facts = await list_newest_notes(engine, limit=10, note_type="fact")
    assert [n.id for n in facts] == ["kno_new", "kno_old"]


async def test_fts_title_ranks_above_text(databases: Databases) -> None:
    project_id = "prj_ftsrank"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                [
                    {
                        "id": "kno_title_match",
                        "type": "runbook",
                        "title": "Kubernetes Cluster Deployment",
                        "text": "General maintenance procedures",
                        "source": None,
                        "author": "admin",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                    {
                        "id": "kno_text_match",
                        "type": "runbook",
                        "title": "System Overview",
                        "text": "This discusses kubernetes cluster deployment steps in details",
                        "source": None,
                        "author": "admin",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                ]
            )
        )

    q = build_fts_query("kubernetes cluster deployment")
    assert q is not None
    candidates = await query_fts_candidates(engine, q, limit=10)
    # Because title is weighted 5.0 and text 1.0, title match must rank first
    assert candidates == ["kno_title_match", "kno_text_match"]


async def test_fts_update_and_delete_triggers(databases: Databases) -> None:
    project_id = "prj_ftstriggers"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                id="kno_changing",
                type="fact",
                title="Alpha Architecture",
                text="Details about alpha pipeline",
                source=None,
                author="user",
                created_at="2026-10-28T10:00:00Z",
                updated_at="2026-10-28T10:00:00Z",
            )
        )

    # Matches alpha
    q_alpha = build_fts_query("alpha")
    assert q_alpha is not None
    assert await query_fts_candidates(engine, q_alpha, limit=10) == ["kno_changing"]

    # Update note to Beta
    async with engine.begin() as conn:
        await conn.execute(
            update(notes)
            .where(notes.c.id == "kno_changing")
            .values(
                title="Beta Architecture",
                text="Details about beta pipeline",
                updated_at="2026-10-28T10:05:00Z",
            )
        )

    # No longer matches alpha, matches beta
    assert await query_fts_candidates(engine, q_alpha, limit=10) == []
    q_beta = build_fts_query("beta")
    assert q_beta is not None
    assert await query_fts_candidates(engine, q_beta, limit=10) == ["kno_changing"]

    # Delete note
    async with engine.begin() as conn:
        await conn.execute(delete(notes).where(notes.c.id == "kno_changing"))

    # Matches nothing
    assert await query_fts_candidates(engine, q_beta, limit=10) == []


async def test_fts_type_filter(databases: Databases) -> None:
    project_id = "prj_ftstype"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                [
                    {
                        "id": "kno_dec",
                        "type": "decision",
                        "title": "Model Serving Framework",
                        "text": "We chose Triton",
                        "source": None,
                        "author": "lead",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                    {
                        "id": "kno_doc",
                        "type": "doc",
                        "title": "Model Serving Guide",
                        "text": "Guide on how to configure Triton",
                        "source": None,
                        "author": "lead",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                ]
            )
        )

    q = build_fts_query("model serving")
    assert q is not None
    both = await query_fts_candidates(engine, q, limit=10)
    assert len(both) == 2

    only_dec = await query_fts_candidates(engine, q, limit=10, note_type="decision")
    assert only_dec == ["kno_dec"]

    only_doc = await query_fts_candidates(engine, q, limit=10, note_type="doc")
    assert only_doc == ["kno_doc"]


async def test_fts5_rebuild_leaves_results_unchanged(databases: Databases) -> None:
    project_id = "prj_ftsrebuild"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                [
                    {
                        "id": "kno_rb1",
                        "type": "fact",
                        "title": "PyTorch 2.0 release",
                        "text": "Compiled graphs and inductor",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                    {
                        "id": "kno_rb2",
                        "type": "fact",
                        "title": "TensorFlow serving",
                        "text": "Legacy deployment",
                        "source": None,
                        "author": "user",
                        "created_at": "2026-10-28T10:00:00Z",
                        "updated_at": "2026-10-28T10:00:00Z",
                    },
                ]
            )
        )

    q = build_fts_query("pytorch inductor")
    assert q is not None
    before = await query_fts_candidates(engine, q, limit=10)
    assert before == ["kno_rb1"]

    # Run FTS5 rebuild
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')"))

    after = await query_fts_candidates(engine, q, limit=10)
    assert after == before


async def test_fts_survives_vacuum_into_and_copy(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_ftsvacuum"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                id="kno_vac",
                type="fact",
                title="Resilient Knowledge Storage",
                text="Vacuum into testing text",
                source=None,
                author="user",
                created_at="2026-10-28T10:00:00Z",
                updated_at="2026-10-28T10:00:00Z",
            )
        )

    dest_db = tmp_path / "vacuumed.db"
    # Execute VACUUM INTO
    async with engine.connect() as conn:
        # Pass path escaped/formatted for SQLite VACUUM INTO
        clean_path = str(dest_db).replace("'", "''")
        await conn.execute(text(f"VACUUM INTO '{clean_path}'"))

    # Connect to the vacuumed copy
    copy_engine = create_engine_for(dest_db, vectors=True)
    try:
        q = build_fts_query("resilient storage")
        assert q is not None
        hits = await query_fts_candidates(copy_engine, q, limit=10)
        assert hits == ["kno_vac"]
    finally:
        await copy_engine.dispose()


async def test_fts_non_ascii_words_match(databases: Databases) -> None:
    project_id = "prj_ftsunicode"
    engine = await databases.project(project_id)

    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                id="kno_unicode",
                type="fact",
                title="Überprüfung der Modelle",
                text="Résultat d'évaluation et données fiables",
                source=None,
                author="user",
                created_at="2026-10-28T10:00:00Z",
                updated_at="2026-10-28T10:00:00Z",
            )
        )

    q1 = build_fts_query("überprüfung")
    assert q1 is not None
    assert await query_fts_candidates(engine, q1, limit=10) == ["kno_unicode"]

    q2 = build_fts_query("résultat")
    assert q2 is not None
    assert await query_fts_candidates(engine, q2, limit=10) == ["kno_unicode"]
