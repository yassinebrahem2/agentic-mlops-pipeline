from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.knowledge.contract import NoteDraft
from mlops_agents.knowledge.limits import TEXT_MAX_CHARS, TITLE_MAX_CHARS
from mlops_agents.knowledge.service import KnowledgeService
from mlops_agents.knowledge.vectors import SqliteVectorStore
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events
from tests.knowledge.fakes import FakeEmbedders, no_secrets


class SpyVectorStore:
    """Mock VectorStore recording method calls."""

    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []

    async def upsert(
        self, project_id: str, note_id: str, vector: list[float], embedder: str
    ) -> None:
        pass

    async def remove(self, project_id: str, note_id: str) -> None:
        self.removed.append((project_id, note_id))

    async def nearest(
        self, project_id: str, vector: list[float], embedder: str, k: int
    ) -> list[str]:
        return []

    async def drop_embedder(self, project_id: str, embedder: str) -> None:
        pass


@pytest.fixture
def fake_embedders() -> FakeEmbedders:
    return FakeEmbedders()


@pytest.fixture
def spy_vectors() -> SpyVectorStore:
    return SpyVectorStore()


async def test_create_note_lifecycle_and_event(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno1"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    draft = NoteDraft(
        type="fact",
        title="Production Deployment Process",
        text="All deployments require approval before rolling out.",
        source="https://wiki.example.com",
    )

    created = await service.save(project_id, "user_alice", draft)
    assert created.id.startswith("kno_")
    assert created.type == "fact"
    assert created.title == draft.title
    assert created.text == draft.text
    assert created.source == draft.source
    assert created.author == "user_alice"
    assert created.created == created.updated

    # Verify event recorded
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(select(events).where(events.c.subject_id == created.id))
        row = res.mappings().first()
        assert row is not None
        assert row["type"] == "note.saved"
        assert row["source"] == "user_alice"
        payload = json.loads(row["payload"])
        assert payload["note_id"] == created.id
        assert payload["type"] == "fact"
        assert payload["title"] == draft.title
        # Critical rule: note text is NEVER in event payload!
        assert "text" not in payload
        assert "deployments require" not in str(payload)


async def test_update_note_keeps_created_and_author_and_removes_old_vector(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno2"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    # 1. Create
    initial = await service.save(
        project_id,
        "author_bob",
        NoteDraft(type="decision", title="Initial Decision", text="Initial content"),
    )

    # 2. Update
    updated = await service.save(
        project_id,
        "editor_charlie",
        NoteDraft(type="runbook", title="Updated Runbook", text="Updated procedures"),
        note_id=initial.id,
    )

    assert updated.id == initial.id
    assert updated.type == "runbook"
    assert updated.title == "Updated Runbook"
    assert updated.text == "Updated procedures"
    assert updated.author == "author_bob"  # author preserved from creator
    assert updated.created == initial.created  # created_at preserved
    assert updated.updated >= initial.updated  # updated_at changed

    # Old vector was removed before updating note
    assert (project_id, initial.id) in spy_vectors.removed

    # Verify update event
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(
            select(events).where(events.c.subject_id == initial.id).order_by(events.c.ts.asc())
        )
        rows = res.mappings().all()
        assert len(rows) == 2
        update_evt = rows[1]
        assert update_evt["type"] == "note.saved"
        assert update_evt["source"] == "editor_charlie"
        payload = json.loads(update_evt["payload"])
        assert payload["title"] == "Updated Runbook"
        assert "text" not in payload


async def test_unknown_note_id_raises_404(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno3"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    # Update non-existent
    with pytest.raises(DomainError) as exc_info:
        await service.save(
            project_id,
            "user",
            NoteDraft(type="fact", title="T", text="C"),
            note_id="kno_missing",
        )
    assert exc_info.value.status == 404
    assert exc_info.value.code == "note_not_found"

    # Delete non-existent
    with pytest.raises(DomainError) as exc_info2:
        await service.delete(project_id, "kno_missing")
    assert exc_info2.value.status == 404
    assert exc_info2.value.code == "note_not_found"


async def test_delete_note_removes_vector_and_records_event(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno4"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    note = await service.save(
        project_id,
        "author",
        NoteDraft(type="incident", title="Outage Postmortem", text="Outage details"),
    )

    await service.delete(project_id, note.id)

    # Note is deleted
    assert await service.get(project_id, note.id) is None

    # Vector was removed
    assert (project_id, note.id) in spy_vectors.removed

    # note.deleted event was recorded
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(
            select(events).where(events.c.type == "note.deleted", events.c.subject_id == note.id)
        )
        row = res.mappings().first()
        assert row is not None
        assert row["source"] == "user"
        payload = json.loads(row["payload"])
        assert payload["note_id"] == note.id
        assert payload["type"] == "incident"


async def test_validation_errors_refuse_save_and_record_no_event(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno5"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    # 1. Overlong title
    long_title = "x" * (TITLE_MAX_CHARS + 1)
    with pytest.raises(DomainError) as exc:
        await service.save(project_id, "user", NoteDraft(type="fact", title=long_title, text="ok"))
    assert exc.value.status == 422
    assert exc.value.code == "note_too_long"

    # 2. Overlong text
    long_text = "y" * (TEXT_MAX_CHARS + 1)
    with pytest.raises(DomainError) as exc:
        await service.save(project_id, "user", NoteDraft(type="fact", title="ok", text=long_text))
    assert exc.value.status == 422
    assert exc.value.code == "note_too_long"

    # 3. Empty title
    with pytest.raises(DomainError) as exc:
        await service.save(project_id, "user", NoteDraft(type="fact", title="   ", text="ok"))
    assert exc.value.status == 422
    assert exc.value.code == "invalid_note"

    # 4. Empty text
    with pytest.raises(DomainError) as exc:
        await service.save(project_id, "user", NoteDraft(type="fact", title="ok", text="   \n"))
    assert exc.value.status == 422
    assert exc.value.code == "invalid_note"

    # Verify no events recorded
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        res = await conn.execute(select(events))
        assert len(res.fetchall()) == 0


async def test_secret_scrubbing_in_save(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno6"
    event_log = SqliteEventLog(databases)

    secret_key = "super_secret_api_token_12345"

    async def get_secrets() -> list[str]:
        return [secret_key]

    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, get_secrets)

    draft = NoteDraft(
        type="doc",
        title=f"Configuration with {secret_key}",
        text=f"Use key {secret_key} and URL https://user:pass@api.service.com/path?token=ghp_abc123",
        source=f"https://source.internal?token={secret_key}",
    )

    saved = await service.save(project_id, "user", draft)

    # Secrets are masked in returned view
    assert secret_key not in saved.title
    assert "***" in saved.title
    assert secret_key not in saved.text
    assert "***" in saved.text
    assert "token=***" in saved.text
    assert "***@" in saved.text
    assert saved.source is not None
    assert secret_key not in saved.source
    assert "***" in saved.source

    # Secrets are masked in database
    retrieved = await service.get(project_id, saved.id)
    assert retrieved is not None
    assert secret_key not in retrieved.title
    assert secret_key not in retrieved.text


async def test_search_empty_query_returns_newest_notes(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno7"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    n1 = await service.save(
        project_id, "u", NoteDraft(type="fact", title="First Note", text="Body 1")
    )
    n2 = await service.save(
        project_id, "u", NoteDraft(type="decision", title="Second Note", text="Body 2")
    )

    # Empty query returns newest first
    res = await service.search(project_id, query="", type=None, limit=10)
    assert res.used_vectors is False
    assert res.detail is None
    assert [n.id for n in res.notes] == [n2.id, n1.id]

    # Whitespace only
    res_ws = await service.search(project_id, query="   ", type=None, limit=10)
    assert [n.id for n in res_ws.notes] == [n2.id, n1.id]

    # Type filtered
    res_fact = await service.search(project_id, query="", type="fact", limit=10)
    assert [n.id for n in res_fact.notes] == [n1.id]


async def test_search_fts_no_embedder_detail_and_limit_clamping(
    databases: Databases, fake_embedders: FakeEmbedders, spy_vectors: SpyVectorStore
) -> None:
    project_id = "prj_kno8"
    event_log = SqliteEventLog(databases)
    service = KnowledgeService(databases, fake_embedders, spy_vectors, event_log, no_secrets)

    await service.save(
        project_id,
        "u",
        NoteDraft(
            type="doc",
            title="Kubernetes Ingress Architecture",
            text="How ingress routes cluster traffic",
        ),
    )

    # 1. Without embedder, detail explains no embedder
    res = await service.search(project_id, query="ingress cluster", type=None, limit=100)
    assert res.used_vectors is False
    assert res.detail is not None
    assert "No embedder is set" in res.detail
    assert len(res.notes) == 1
    assert res.notes[0].title == "Kubernetes Ingress Architecture"

    # 2. When fake embedders has a problem configured
    fake_embedders.set_problem("Provider key exhausted")
    res_problem = await service.search(project_id, query="ingress", type=None, limit=5)
    assert res_problem.detail == "Provider key exhausted"

    # 3. Limit clamped to SEARCH_MAX_LIMIT (20)
    # Testing clamp on query with limit <= 0 -> clamps to 1
    res_zero = await service.search(project_id, query="", type=None, limit=-5)
    assert len(res_zero.notes) == 1


async def test_service_with_sqlite_vector_store(
    databases: Databases, fake_embedders: FakeEmbedders
) -> None:
    # Test real SqliteVectorStore integration
    project_id = "prj_kno9"
    event_log = SqliteEventLog(databases)
    real_vectors = SqliteVectorStore(databases)
    service = KnowledgeService(databases, fake_embedders, real_vectors, event_log, no_secrets)

    note = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Vector Integrated Note", text="Some text"),
    )
    assert note.id.startswith("kno_")

    # Update note removes vector without error
    updated = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Updated Vector Note", text="Updated text"),
        note_id=note.id,
    )
    assert updated.title == "Updated Vector Note"

    # Delete note removes vector without error
    await service.delete(project_id, note.id)
    assert await service.get(project_id, note.id) is None
