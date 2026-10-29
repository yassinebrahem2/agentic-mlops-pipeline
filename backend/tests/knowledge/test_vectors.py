from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, select, text

from mlops_agents.knowledge.limits import MAX_DIMENSIONS
from mlops_agents.knowledge.vectors import SqliteVectorStore, VectorStoreError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.backup import backup_db, snapshot_all
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import create_engine_for
from mlops_agents.storage.tables import note_embeddings, notes


async def _create_note(
    databases: Databases, project_id: str, note_id: str, title: str = "Note"
) -> None:
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(notes).values(
                id=note_id,
                type="fact",
                title=title,
                text="Content for note",
                source=None,
                author="tester",
                created_at="2026-10-28T12:00:00Z",
                updated_at="2026-10-28T12:00:00Z",
            )
        )


async def test_upsert_unknown_note_raises_vector_store_error(databases: Databases) -> None:
    store = SqliteVectorStore(databases)
    with pytest.raises(VectorStoreError) as exc_info:
        await store.upsert("prj_kno1", "kno_missing", [0.1, 0.2, 0.3], "fake:embed@3")
    assert "Note not found" in str(exc_info.value)


async def test_remove_unknown_note_is_noop(databases: Databases) -> None:
    store = SqliteVectorStore(databases)
    # Should not raise any error
    await store.remove("prj_kno1", "kno_nonexistent")


async def test_nearest_maps_hits_back_to_note_ids_in_rank_order(databases: Databases) -> None:
    project_id = "prj_knn1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_1", "First")
    await _create_note(databases, project_id, "kno_2", "Second")
    await _create_note(databases, project_id, "kno_3", "Third")

    await store.upsert(project_id, "kno_1", [1.0, 0.0, 0.0], "fake:embed@3")
    await store.upsert(project_id, "kno_2", [0.0, 1.0, 0.0], "fake:embed@3")
    await store.upsert(project_id, "kno_3", [0.0, 0.0, 1.0], "fake:embed@3")

    hits = await store.nearest(project_id, [0.9, 0.1, 0.0], "fake:embed@3", k=2)
    assert hits == ["kno_1", "kno_2"]


async def test_nearest_orders_by_cosine_similarity(databases: Databases) -> None:
    project_id = "prj_cosine1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_a", "A")
    await _create_note(databases, project_id, "kno_b", "B")
    await _create_note(databases, project_id, "kno_c", "C")

    # A: [1, 0], B: [0.707, 0.707], C: [0, 1]
    await store.upsert(project_id, "kno_a", [1.0, 0.0], "embed@2")
    await store.upsert(project_id, "kno_b", [0.7071, 0.7071], "embed@2")
    await store.upsert(project_id, "kno_c", [0.0, 1.0], "embed@2")

    # Query close to C
    hits_c = await store.nearest(project_id, [0.1, 0.9], "embed@2", k=3)
    assert hits_c == ["kno_c", "kno_b", "kno_a"]

    # Query close to A
    hits_a = await store.nearest(project_id, [0.95, 0.05], "embed@2", k=3)
    assert hits_a == ["kno_a", "kno_b", "kno_c"]


async def test_second_upsert_same_note_leaves_one_row(databases: Databases) -> None:
    project_id = "prj_upsert1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_1", "First")

    await store.upsert(project_id, "kno_1", [1.0, 0.0], "embed@2")
    # Change dimension and embedder
    await store.upsert(project_id, "kno_1", [0.0, 1.0, 0.0], "embed@3")

    # Verify note_embeddings has exactly 1 row
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(
            select(note_embeddings).where(note_embeddings.c.note_id == "kno_1")
        )
        rows = res.fetchall()
        assert len(rows) == 1
        assert rows[0].dimensions == 3
        assert rows[0].embedder == "embed@3"

    # Old table note_vec_2 no longer has the vector
    hits_2 = await store.nearest(project_id, [1.0, 0.0], "embed@2", k=10)
    assert hits_2 == []

    # New table note_vec_3 has it
    hits_3 = await store.nearest(project_id, [0.0, 1.0, 0.0], "embed@3", k=10)
    assert hits_3 == ["kno_1"]


async def test_embedded_at_is_set_on_upsert(databases: Databases) -> None:
    project_id = "prj_embedded1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_ts", "Timestamped")

    await store.upsert(project_id, "kno_ts", [0.5, 0.5], "embed@2")
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(
            select(note_embeddings.c.embedded_at).where(note_embeddings.c.note_id == "kno_ts")
        )
        embedded_at = res.scalar()
        assert embedded_at is not None
        assert "T" in embedded_at


async def test_two_dimensions_use_two_tables(databases: Databases) -> None:
    project_id = "prj_dims1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_2d", "2-dim")
    await _create_note(databases, project_id, "kno_4d", "4-dim")

    await store.upsert(project_id, "kno_2d", [1.0, 0.0], "dim2@2")
    await store.upsert(project_id, "kno_4d", [0.0, 1.0, 0.0, 0.0], "dim4@4")

    # 2d query never returns 4d note
    hits_2 = await store.nearest(project_id, [1.0, 0.0], "dim2@2", k=5)
    assert hits_2 == ["kno_2d"]

    # 4d query never returns 2d note
    hits_4 = await store.nearest(project_id, [0.0, 1.0, 0.0, 0.0], "dim4@4", k=5)
    assert hits_4 == ["kno_4d"]


async def test_embedder_filters_in_same_dimension(databases: Databases) -> None:
    project_id = "prj_filters1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_prov_a", "Provider A")
    await _create_note(databases, project_id, "kno_prov_b", "Provider B")

    await store.upsert(project_id, "kno_prov_a", [1.0, 0.0], "provider_a:model@2")
    await store.upsert(project_id, "kno_prov_b", [1.0, 0.0], "provider_b:model@2")

    hits_a = await store.nearest(project_id, [1.0, 0.0], "provider_a:model@2", k=5)
    assert hits_a == ["kno_prov_a"]

    hits_b = await store.nearest(project_id, [1.0, 0.0], "provider_b:model@2", k=5)
    assert hits_b == ["kno_prov_b"]


async def test_remove_deletes_row_and_drops_emptied_table(databases: Databases) -> None:
    project_id = "prj_remdrop1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_rem", "Remove Me")

    await store.upsert(project_id, "kno_rem", [1.0, 0.0, 0.0], "rem@3")

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_3'")
        )
        assert res.scalar() == 1

    await store.remove(project_id, "kno_rem")

    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_3'")
        )
        assert res.scalar() == 0
        emb_res = await conn.execute(
            select(note_embeddings).where(note_embeddings.c.note_id == "kno_rem")
        )
        assert emb_res.fetchone() is None


async def test_drop_embedder_deletes_rows_and_drops_emptied_table(databases: Databases) -> None:
    project_id = "prj_dropemb1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_a1", "A1")
    await _create_note(databases, project_id, "kno_a2", "A2")
    await _create_note(databases, project_id, "kno_b1", "B1")

    await store.upsert(project_id, "kno_a1", [1.0, 0.0], "embed_a@2")
    await store.upsert(project_id, "kno_a2", [0.0, 1.0], "embed_a@2")
    await store.upsert(project_id, "kno_b1", [0.5, 0.5], "embed_b@2")

    engine = await databases.project(project_id)

    # Drop embed_a
    await store.drop_embedder(project_id, "embed_a@2")

    # Table note_vec_2 still exists because embed_b1 is there
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_2'")
        )
        assert res.scalar() == 1

    hits = await store.nearest(project_id, [1.0, 0.0], "embed_a@2", k=5)
    assert hits == []
    hits_b = await store.nearest(project_id, [0.5, 0.5], "embed_b@2", k=5)
    assert hits_b == ["kno_b1"]

    # Now drop embed_b
    await store.drop_embedder(project_id, "embed_b@2")

    # Table note_vec_2 should now be dropped
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_2'")
        )
        assert res.scalar() == 0


async def test_bad_or_empty_vector_raises_vector_store_error(databases: Databases) -> None:
    project_id = "prj_badvec1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_val", "Valid Note")

    # Empty vector
    with pytest.raises(VectorStoreError):
        await store.upsert(project_id, "kno_val", [], "embed@0")

    with pytest.raises(VectorStoreError):
        await store.nearest(project_id, [], "embed@0", k=5)

    # NaN vector
    with pytest.raises(VectorStoreError):
        await store.upsert(project_id, "kno_val", [float("nan"), 1.0], "embed@2")

    # Inf vector
    with pytest.raises(VectorStoreError):
        await store.upsert(project_id, "kno_val", [float("inf"), 1.0], "embed@2")


async def test_vector_exceeding_max_dimensions_refused_before_sql(databases: Databases) -> None:
    project_id = "prj_maxdim1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_max", "Max Note")

    huge_vec = [0.1] * (MAX_DIMENSIONS + 1)
    with pytest.raises(VectorStoreError) as exc_info:
        await store.upsert(project_id, "kno_max", huge_vec, f"embed@{MAX_DIMENSIONS + 1}")
    assert "dimensions" in str(exc_info.value).lower()


async def test_vector_max_dimensions_works(databases: Databases) -> None:
    project_id = "prj_dim8192"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_max8192", "Max Dimensions Note")

    max_vec = [0.01] * MAX_DIMENSIONS
    key = f"embed@{MAX_DIMENSIONS}"
    await store.upsert(project_id, "kno_max8192", max_vec, key)

    nearest = await store.nearest(project_id, max_vec, key, k=5)
    assert nearest == ["kno_max8192"]


async def test_missing_extension_raises_vector_store_error_naming_unavailable(
    databases: Databases, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_id = "prj_noext1"
    # Ensure project engine is disposed so we get a new connection with missing extension
    await databases.close_project(project_id)

    monkeypatch.setattr(
        "mlops_agents.storage.vector_extension.sqlite_vec.loadable_path",
        lambda: str(tmp_path / "nonexistent_vec0"),
    )

    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_fail", "Fail Note")

    with pytest.raises(VectorStoreError) as exc_info:
        await store.upsert(project_id, "kno_fail", [0.1, 0.2], "embed@2")
    assert "not available on this installation" in str(exc_info.value).lower()


async def test_backup_and_snapshot_succeed_and_copy_answers_nearest(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_backup1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_bak", "Backup Note")
    await store.upsert(project_id, "kno_bak", [0.0, 1.0], "embed@2")

    db_path = paths.project_db(project_id)
    backup_dir = paths.backups / "test_backup"
    backed_up_file = backup_db(db_path, backup_dir)
    assert backed_up_file.is_file()

    snap_dir = snapshot_all(paths)
    assert snap_dir.is_dir()
    snap_db = snap_dir / "projects" / f"{project_id}.db"
    assert snap_db.is_file()

    # Open the backed up database with an engine with vectors=True
    copied_engine = create_engine_for(backed_up_file, vectors=True)
    async with copied_engine.connect() as conn:
        res = await conn.execute(text("SELECT vec_version()"))
        assert res.scalar() is not None

    # Verify a new Databases instance pointing to the backup can query nearest
    backup_paths = DataPaths(backup_dir.parent)
    target_dir = backup_paths.projects / project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(backed_up_file, target_dir / "project.db")
    backup_dbs = Databases(backup_paths)
    backup_store = SqliteVectorStore(backup_dbs)
    hits = await backup_store.nearest(project_id, [0.0, 1.0], "embed@2", k=1)
    assert hits == ["kno_bak"]
    await backup_dbs.dispose()
    await copied_engine.dispose()


async def test_migrate_applies_later_migration_over_database_with_vec_tables(
    databases: Databases, tmp_path: Path
) -> None:
    project_id = "prj_migration1"
    store = SqliteVectorStore(databases)
    await _create_note(databases, project_id, "kno_mig", "Mig Note")
    await store.upsert(project_id, "kno_mig", [1.0, 0.0], "embed@2")

    # Close project engine so file lock is released
    await databases.close_project(project_id)

    db_path = databases.paths.project_db(project_id)
    migrations_dir = tmp_path / "custom_migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)

    # Copy existing migrations
    from importlib.resources import files

    source_folder = files("mlops_agents.storage").joinpath("migrations", "project")
    max_version = 0
    for entry in source_folder.iterdir():
        (migrations_dir / entry.name).write_text(entry.read_text("utf-8"), encoding="utf-8")
        if entry.name[:4].isdigit():
            max_version = max(max_version, int(entry.name[:4]))

    # Add next migration that creates, drops, and renames a table (the stage 2 pattern)
    next_version = max_version + 1
    m_next = f"""-- migration {next_version:04d}: test rebuild table
CREATE TABLE dummy_new (id TEXT PRIMARY KEY, val TEXT);
INSERT INTO dummy_new VALUES ('1', 'hello');
DROP TABLE IF EXISTS dummy;
ALTER TABLE dummy_new RENAME TO dummy;
"""
    (migrations_dir / f"{next_version:04d}_rebuild_dummy.sql").write_text(m_next, encoding="utf-8")

    applied = migrate(
        db_path,
        "project",
        tmp_path / "backups",
        source=migrations_dir,
    )
    assert next_version in applied

    # Reconnect directly with vectors=True and verify vec0 still works and dummy exists
    migrated_engine = create_engine_for(db_path, vectors=True)
    async with migrated_engine.connect() as conn:
        res = await conn.execute(text("SELECT val FROM dummy WHERE id = '1'"))
        assert res.scalar() == "hello"

        # Verify vec table is still intact and queried
        tbl_check = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'note_vec_2'")
        )
        assert tbl_check.scalar() == 1
    await migrated_engine.dispose()
