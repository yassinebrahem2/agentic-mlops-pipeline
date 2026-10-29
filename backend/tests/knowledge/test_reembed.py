"""Tests for re-embedding passes, embedder changes, and pass concurrency (I17)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import text

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.knowledge.contract import NoteDraft
from mlops_agents.knowledge.embedding import ReembedPass, embedder_key, run_reembed_pass
from mlops_agents.knowledge.service import KnowledgeService
from mlops_agents.knowledge.state import get_vectors_ready_for
from mlops_agents.knowledge.vectors import SqliteVectorStore
from mlops_agents.storage.databases import Databases
from tests.knowledge.fakes import ConceptEmbedder, FakeEmbedders, no_secrets


class CallbackEmbedder(ConceptEmbedder):
    """ConceptEmbedder invoking an optional hook during embed()."""

    def __init__(
        self,
        address: str,
        dimensions: int,
        concepts: dict[str, set[str]],
        *,
        on_embed: Callable[[Sequence[str]], Any] | None = None,
    ) -> None:
        super().__init__(address, dimensions, concepts)
        self.on_embed = on_embed

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.on_embed is not None:
            res = self.on_embed(texts)
            if asyncio.iscoroutine(res):
                await res
        return await super().embed(texts)


async def test_changing_embedder_lifecycle(databases: Databases) -> None:
    project_id = "prj_reembed1"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    emb_v1 = ConceptEmbedder("model-v1", 2, {"vehicle": {"car", "truck"}})
    emb_v2 = ConceptEmbedder("model-v2", 4, {"vehicle": {"car", "automobile"}})

    fake_embedders = FakeEmbedders(emb_v1)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    note = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Fleet", text="We drive a car for inspections."),
    )
    await service.wait_idle(project_id)

    # First search is hybrid with v1
    res1 = await service.search(project_id, query="truck", type=None, limit=5)
    assert res1.used_vectors is True
    assert res1.detail is None

    # Switch to emb_v2 (different address and different dimensions)
    fake_embedders.set_embedder(project_id, emb_v2)

    # Immediate search returns full-text with detail f"Notes are being re-embedded with model-v2..."
    res2 = await service.search(project_id, query="truck", type=None, limit=5)
    assert res2.used_vectors is False
    assert res2.detail is not None
    assert f"Notes are being re-embedded with {emb_v2.address}" in res2.detail

    # Wait for re-embedding to finish
    await service.wait_idle(project_id)

    # After wait_idle:
    # 1. Ready key is emb_v2
    ready_key = await get_vectors_ready_for(databases, project_id)
    assert ready_key == embedder_key(emb_v2.address, emb_v2.dimensions)

    # 2. note_embeddings table holds ONLY the new key
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        emb_res = await conn.execute(text("SELECT DISTINCT embedder FROM note_embeddings"))
        keys = [r[0] for r in emb_res.fetchall()]
        assert keys == [embedder_key(emb_v2.address, emb_v2.dimensions)]

        # 3. Old note_vec_2 virtual table is dropped
        tbl_res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_2'")
        )
        assert tbl_res.scalar() == 0

        # 4. New note_vec_4 virtual table exists
        tbl4_res = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_4'")
        )
        assert tbl4_res.scalar() == 1

    # 5. Hybrid search is answered with v2 without dimension errors
    res3 = await service.search(project_id, query="automobile", type=None, limit=5)
    assert res3.used_vectors is True
    assert res3.detail is None
    assert len(res3.notes) == 1
    assert res3.notes[0].id == note.id


async def test_changing_embedder_same_address_new_dimension(databases: Databases) -> None:
    project_id = "prj_samedim"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    emb_2d = ConceptEmbedder("common-model", 2, {"vehicle": {"car"}})
    emb_4d = ConceptEmbedder("common-model", 4, {"vehicle": {"car"}})

    fake_embedders = FakeEmbedders(emb_2d)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Note 1", text="car operations"),
    )
    await service.wait_idle(project_id)

    # Switch to same address but 4 dimensions
    fake_embedders.set_embedder(project_id, emb_4d)
    service.kick(project_id)
    await service.wait_idle(project_id)

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        emb_res = await conn.execute(text("SELECT DISTINCT embedder FROM note_embeddings"))
        keys = [r[0] for r in emb_res.fetchall()]
        assert keys == ["common-model@4"]

        tbl2 = await conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'note_vec_2'")
        )
        assert tbl2.scalar() == 0


async def test_notes_saved_during_pass_embedded_by_rerun(databases: Databases) -> None:
    project_id = "prj_rerun1"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    saved_note_2: list[str] = []
    service_ref: list[KnowledgeService] = []

    async def on_embed_callback(texts: Sequence[str]) -> None:
        if not saved_note_2 and service_ref:
            # Concurrently save a second note while pass is embedding batch
            n2 = await service_ref[0].save(
                project_id,
                "bob",
                NoteDraft(type="fact", title="Second Note", text="A truck on the road."),
            )
            saved_note_2.append(n2.id)

    emb = CallbackEmbedder(
        "model-cb",
        2,
        {"vehicle": {"car", "truck"}},
        on_embed=on_embed_callback,
    )
    fake_embedders = FakeEmbedders(emb)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)
    service_ref.append(service)

    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="First Note", text="A car in the driveway."),
    )

    await service.wait_idle(project_id)

    # Both notes should have been embedded by the rerun loop
    assert len(saved_note_2) == 1
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT count(*) FROM note_embeddings"))
        assert res.scalar() == 2

    # Both are findable by vector search
    res = await service.search(project_id, query="truck", type=None, limit=10)
    assert res.used_vectors is True
    ids = [n.id for n in res.notes]
    assert saved_note_2[0] in ids


async def test_note_edited_while_pass_holds_old_text(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_raceedit"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    service_ref: list[KnowledgeService] = []
    target_note_id: list[str] = []
    edited = False

    # Freeze now_iso so timestamps are identical
    frozen_ts = "2026-10-28T12:00:00.000000+00:00"
    monkeypatch.setattr("mlops_agents.knowledge.service.now_iso", lambda: frozen_ts)

    async def on_embed_callback(texts: Sequence[str]) -> None:
        nonlocal edited
        if not edited and target_note_id and service_ref:
            edited = True
            # Edit note while pass holds old text
            await service_ref[0].save(
                project_id,
                "alice",
                NoteDraft(
                    type="fact",
                    title="Target Note",
                    text="Completely new text about trucks and motors.",
                ),
                note_id=target_note_id[0],
            )

    emb = CallbackEmbedder(
        "model-race",
        2,
        {"vehicle": {"truck", "motor"}},
        on_embed=on_embed_callback,
    )
    fake_embedders = FakeEmbedders(emb)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)
    service_ref.append(service)

    n1 = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Target Note", text="Old text with no vehicle concepts."),
    )
    target_note_id.append(n1.id)

    await service.wait_idle(project_id)

    # Note was edited and re-embedded with new text through rerun
    res = await service.search(project_id, query="motor", type=None, limit=5)
    assert res.used_vectors is True
    assert len(res.notes) == 1
    assert res.notes[0].id == n1.id
    assert "trucks and motors" in res.notes[0].text


async def test_note_deleted_while_pass_holds_it(databases: Databases) -> None:
    project_id = "prj_racedelete"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    service_ref: list[KnowledgeService] = []
    note_id: list[str] = []
    deleted = False

    async def on_embed_callback(texts: Sequence[str]) -> None:
        nonlocal deleted
        if not deleted and note_id and service_ref:
            deleted = True
            await service_ref[0].delete(project_id, note_id[0])

    emb = CallbackEmbedder(
        "model-del",
        2,
        {"vehicle": {"car"}},
        on_embed=on_embed_callback,
    )
    fake_embedders = FakeEmbedders(emb)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)
    service_ref.append(service)

    n = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Will Be Deleted", text="car text"),
    )
    note_id.append(n.id)

    # Pass should finish cleanly without error
    await service.wait_idle(project_id)

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT count(*) FROM note_embeddings"))
        assert res.scalar() == 0


async def test_update_note_vector_lifecycle(databases: Databases) -> None:
    project_id = "prj_lifecycle"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    emb = ConceptEmbedder(
        "model-life",
        2,
        {"vehicle": {"car", "automobile"}, "fruit": {"apple", "banana"}},
    )
    fake_embedders = FakeEmbedders(emb)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    note = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Vehicle Note", text="Driving a car."),
    )
    await service.wait_idle(project_id)

    # Found by meaning
    res1 = await service.search(project_id, query="automobile", type=None, limit=5)
    assert res1.used_vectors is True
    assert len(res1.notes) == 1

    # Update note to fruit concept
    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Fruit Note", text="Eating a banana."),
        note_id=note.id,
    )

    # Old vector is gone at once before pass runs!
    # "automobile" will not match vector or text now
    res_interim = await service.search(project_id, query="automobile", type=None, limit=5)
    assert len(res_interim.notes) == 0

    # New text matches through FTS
    res_fts = await service.search(project_id, query="banana", type=None, limit=5)
    assert len(res_fts.notes) == 1

    # After pass completes, found by meaning ("apple" matches concept fruit)
    await service.wait_idle(project_id)
    res_meaning = await service.search(project_id, query="apple", type=None, limit=5)
    assert res_meaning.used_vectors is True
    assert len(res_meaning.notes) == 1

    # Delete note: removes vector and bookkeeping row
    await service.delete(project_id, note.id)
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT count(*) FROM note_embeddings"))
        assert res.scalar() == 0


async def test_close_cancels_running_pass_and_new_service_resumes(databases: Databases) -> None:
    project_id = "prj_closecancel"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)

    started_embed = asyncio.Event()
    block_embed = asyncio.Event()

    async def slow_embed(texts: Sequence[str]) -> None:
        started_embed.set()
        await block_embed.wait()

    emb = CallbackEmbedder(
        "model-slow",
        2,
        {"concept": {"alpha"}},
        on_embed=slow_embed,
    )
    fake_embedders = FakeEmbedders(emb)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Pending Note", text="alpha text"),
    )

    # Wait until embedder has started
    await started_embed.wait()

    # Close service: cancels running pass cleanly
    await service.close()

    # Ready key was NOT set because pass was cancelled
    ready_key = await get_vectors_ready_for(databases, project_id)
    assert ready_key is None

    # New service over same DB resumes pending notes
    emb_resumed = ConceptEmbedder("model-slow", 2, {"concept": {"alpha"}})
    fake_embedders_resumed = FakeEmbedders(emb_resumed)
    service2 = KnowledgeService(databases, fake_embedders_resumed, vectors, event_log, no_secrets)

    service2.kick(project_id)
    await service2.wait_idle(project_id)

    # Ready key is now set
    ready_key2 = await get_vectors_ready_for(databases, project_id)
    assert ready_key2 == embedder_key(emb_resumed.address, emb_resumed.dimensions)

    # Note is now embedded
    res = await service2.search(project_id, query="alpha", type=None, limit=5)
    assert res.used_vectors is True
    assert len(res.notes) == 1


async def test_embedding_pass_failing_sql_stores_driver_message_without_sql_text(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_sqlerr"
    vectors = SqliteVectorStore(databases)
    embedder = ConceptEmbedder("model-err", 2, {"c": {"a"}})
    fake_embedders = FakeEmbedders(embedder)
    pass_state = ReembedPass()

    real_engine = await databases.project(project_id)

    class ProxyEngine:
        def __getattr__(self, name: str) -> Any:
            return getattr(real_engine, name)

        def connect(self) -> Any:
            conn_cm = real_engine.connect()

            class ConnProxy:
                async def __aenter__(self) -> Any:
                    conn = await conn_cm.__aenter__()
                    try:
                        await conn.execute(
                            text("SELECT * FROM non_existent_table_in_embedding_pass")
                        )
                    except BaseException as exc:
                        await conn_cm.__aexit__(type(exc), exc, exc.__traceback__)
                        raise
                    return conn

                async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
                    return await conn_cm.__aexit__(exc_type, exc_val, exc_tb)

            return ConnProxy()

    async def fake_project(pid: str) -> Any:
        del pid
        return ProxyEngine()

    monkeypatch.setattr(databases, "project", fake_project)

    locks: dict[str, asyncio.Lock] = {}

    def get_lock(pid: str) -> asyncio.Lock:
        return locks.setdefault(pid, asyncio.Lock())

    await run_reembed_pass(
        project_id,
        pass_state,
        databases=databases,
        embedders=fake_embedders,
        vectors=vectors,
        get_project_lock=get_lock,
        monotonic=time.monotonic,
    )

    assert pass_state.last_failure_msg is not None
    assert "[SQL:" not in pass_state.last_failure_msg
    assert "no such table: non_existent_table_in_embedding_pass" in pass_state.last_failure_msg
