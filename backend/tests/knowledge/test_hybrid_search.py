"""Tests for hybrid search combining full-text search and vector similarity (I17)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.knowledge.contract import NoteDraft
from mlops_agents.knowledge.embedding import embedder_key
from mlops_agents.knowledge.service import KnowledgeService
from mlops_agents.knowledge.state import get_vectors_ready_for
from mlops_agents.knowledge.vectors import SqliteVectorStore
from mlops_agents.storage.databases import Databases
from tests.knowledge.fakes import ConceptEmbedder, FakeEmbedders, no_secrets


class NonZeroConceptEmbedder(ConceptEmbedder):
    """ConceptEmbedder ensuring non-zero vectors so sqlite-vec cosine distance is defined."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raw = await super().embed(texts)
        res: list[list[float]] = []
        for vec in raw:
            if not any(v > 0.0 for v in vec):
                v_copy = list(vec)
                v_copy[-1] = 1.0
                res.append(v_copy)
            else:
                res.append(vec)
        return res


@pytest.fixture
def concept_embedder() -> NonZeroConceptEmbedder:
    return NonZeroConceptEmbedder(
        address="concept-model",
        dimensions=4,
        concepts={
            "vehicle": {"car", "automobile", "truck", "motor"},
            "space": {"planet", "orbit", "galaxy", "rocket"},
        },
    )


async def test_hybrid_search_keyword_and_meaning(
    databases: Databases, concept_embedder: NonZeroConceptEmbedder
) -> None:
    project_id = "prj_hybrid1"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    fake_embedders = FakeEmbedders(concept_embedder)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    # Note 1: keyword-only match ("XJ-900" is an identifier, absent from concepts)
    n1 = await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="fact",
            title="Device Specification",
            text="The XJ-900 device operates at 48V nominal voltage.",
        ),
    )

    # Note 2: meaning-only match (note has "car", query will have "automobile")
    n2 = await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="fact",
            title="Fleet Management",
            text="The team uses a car for daily site inspections.",
        ),
    )

    # Note 3: both keyword and meaning match ("XJ-900" and "car")
    n3 = await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="fact",
            title="Prototype Vehicle",
            text="The XJ-900 electric car broke the endurance record.",
        ),
    )

    # Note 4: irrelevant note
    await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="doc",
            title="Kitchen Recipes",
            text="How to bake sourdough bread step by step.",
        ),
    )

    # Wait for re-embed pass to finish embedding all notes
    await service.wait_idle(project_id)

    # Search for "automobile XJ-900"
    # "XJ-900" matches FTS for n1 and n3.
    # "automobile" matches vector concept "vehicle" which is present in n2 and n3 ("car").
    res = await service.search(project_id, query="automobile XJ-900", type=None, limit=10)

    assert res.used_vectors is True
    assert res.detail is None

    result_ids = [n.id for n in res.notes]
    # Note 3 (both keyword and meaning) must rank first
    assert result_ids[0] == n3.id
    # Both keyword-only (n1) and meaning-only (n2) rank in results
    assert n1.id in result_ids
    assert n2.id in result_ids
    assert len(res.notes) >= 3


async def test_hybrid_search_type_filter(
    databases: Databases, concept_embedder: NonZeroConceptEmbedder
) -> None:
    project_id = "prj_hybridtype"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    fake_embedders = FakeEmbedders(concept_embedder)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    n_fact = await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="fact",
            title="Rocket specs",
            text="The rocket weighs 50 tons and carries satellites to orbit.",
        ),
    )

    await service.save(
        project_id,
        "alice",
        NoteDraft(
            type="decision",
            title="Rocket choice",
            text="We decided to use the liquid fueled rocket for this mission.",
        ),
    )

    await service.wait_idle(project_id)

    res = await service.search(project_id, query="planet galaxy", type="fact", limit=10)
    assert res.used_vectors is True
    assert res.detail is None
    assert len(res.notes) == 1
    assert res.notes[0].id == n_fact.id
    assert res.notes[0].type == "fact"


async def test_fresh_project_notes_before_embedder_first_search_then_idle(
    databases: Databases, concept_embedder: NonZeroConceptEmbedder
) -> None:
    project_id = "prj_fresh1"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    # Start with no embedder
    fake_embedders = FakeEmbedders(embedder=None)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Database Config", text="Postgres database settings."),
    )

    # Now an embedder is configured for the project
    fake_embedders.set_embedder(project_id, concept_embedder)

    # First search: embedder not ready yet -> returns full text with re-embed detail, kicks pass
    res1 = await service.search(project_id, query="database", type=None, limit=10)
    assert res1.used_vectors is False
    assert res1.detail is not None
    assert f"Notes are being re-embedded with {concept_embedder.address}" in res1.detail
    assert len(res1.notes) == 1

    # Wait for background pass to finish
    await service.wait_idle(project_id)

    # Check that ready key is now stored in project_settings
    ready_key = await get_vectors_ready_for(databases, project_id)
    expected_key = embedder_key(concept_embedder.address, concept_embedder.dimensions)
    assert ready_key == expected_key

    # Next search is hybrid
    res2 = await service.search(project_id, query="database", type=None, limit=10)
    assert res2.used_vectors is True
    assert res2.detail is None
    assert len(res2.notes) == 1


async def test_exhausted_embedder_fallback_and_cooldown(
    databases: Databases, concept_embedder: NonZeroConceptEmbedder
) -> None:
    project_id = "prj_exhaust1"
    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    fake_embedders = FakeEmbedders(concept_embedder)

    current_time = 1000.0

    def mock_monotonic() -> float:
        return current_time

    service = KnowledgeService(
        databases,
        fake_embedders,
        vectors,
        event_log,
        no_secrets,
        monotonic=mock_monotonic,
    )

    await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Alpha Note", text="Car and truck operations."),
    )
    await service.wait_idle(project_id)

    # Reset call count
    concept_embedder.call_count = 0

    # Exhaust embedder
    concept_embedder.raises_unavailable = True
    concept_embedder.unavailable_message = "Quota exhausted, rate limit hit"

    # Search with exhausted embedder: query embedding raises EmbedderUnavailable
    res = await service.search(project_id, query="truck", type=None, limit=5)
    assert res.used_vectors is False
    assert res.detail == "Quota exhausted, rate limit hit"
    assert len(res.notes) == 1
    assert concept_embedder.call_count == 1

    # Save succeeds even with exhausted embedder
    saved2 = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Beta Note", text="Automobile repair."),
    )
    assert saved2.id.startswith("kno_")

    # Within cooldown: kick in save was a no-op, no second embed call was made!
    assert concept_embedder.call_count == 1

    # Subsequent search within cooldown: returns fallback without calling embedder again
    res2 = await service.search(project_id, query="truck", type=None, limit=5)
    assert res2.used_vectors is False
    assert res2.detail == "Quota exhausted, rate limit hit"
    assert concept_embedder.call_count == 1

    # Now time advances past RETRY_AFTER_SECONDS (60s)
    current_time += 65.0
    concept_embedder.raises_unavailable = False

    # Kick re-embedding and wait idle
    service.kick(project_id)
    await service.wait_idle(project_id)

    # Embedder was called to embed the pending batch
    assert concept_embedder.call_count == 2

    # Now search is hybrid again
    res3 = await service.search(project_id, query="truck", type=None, limit=5)
    assert res3.used_vectors is True
    assert res3.detail is None
    # 1 for query embed in search
    assert concept_embedder.call_count == 3


async def test_vector_store_error_fallback(
    databases: Databases,
    concept_embedder: NonZeroConceptEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_id = "prj_noextservice"
    # Ensure project engine is disposed so we get a fresh connection
    await databases.close_project(project_id)

    # Patch sqlite_vec to point to nonexistent extension
    monkeypatch.setattr(
        "mlops_agents.storage.vector_extension.sqlite_vec.loadable_path",
        lambda: str(tmp_path / "nonexistent_vec0"),
    )

    event_log = SqliteEventLog(databases)
    vectors = SqliteVectorStore(databases)
    fake_embedders = FakeEmbedders(concept_embedder)
    service = KnowledgeService(databases, fake_embedders, vectors, event_log, no_secrets)

    # Saving still works even when vector extension is unavailable
    note = await service.save(
        project_id,
        "alice",
        NoteDraft(type="fact", title="Kubernetes Cluster", text="Cluster configuration notes."),
    )
    assert note.id.startswith("kno_")
    await service.wait_idle(project_id)

    # Search falls back to full-text search with "not available" in detail
    res = await service.search(project_id, query="Cluster", type=None, limit=5)
    assert res.used_vectors is False
    assert res.detail is not None
    assert "not available on this installation" in res.detail.lower()
    assert len(res.notes) == 1
    assert res.notes[0].id == note.id
