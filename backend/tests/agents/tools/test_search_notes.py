from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.policy.table import PermissionLevel, decide
from mlops_agents.agents.tools.base import RiskClass, ToolContext
from mlops_agents.agents.tools.search_notes import SearchNotes
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.knowledge.contract import NoteDraft, NoteView, SearchResult
from mlops_agents.knowledge.service import KnowledgeService
from mlops_agents.storage.databases import Databases
from tests.knowledge.fakes import FakeEmbedders, no_secrets
from tests.knowledge.test_service_notes import SpyVectorStore


class DummyFiles:
    root = Path()

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        pass


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        project_id="prj_test",
        agent="tester",
        session_id="ses_test",
        turn_id="trn_test",
        files=DummyFiles(),
        task=None,
        tool_call_id="call_test",
    )


@pytest.fixture
def knowledge_service(databases: Databases) -> KnowledgeService:
    events = SqliteEventLog(databases)
    return KnowledgeService(
        databases=databases,
        embedders=FakeEmbedders(),
        vectors=SpyVectorStore(),
        events=events,
        secrets=no_secrets,
    )


async def test_search_notes_ranked_output_format(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    long_text = "Detailed paragraph about Kubernetes deployments. " * 20  # > 500 chars
    note1 = await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(
            type="runbook",
            title="Kubernetes Cluster Setup",
            text=long_text,
            source="infra/k8s.md",
        ),
    )
    note2 = await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(
            type="fact",
            title="Kubernetes Cluster Version",
            text="Running v1.29 on AWS EKS.",
            source=None,
        ),
    )

    tool = SearchNotes(knowledge_service)
    out = await tool.invoke(ctx, {"query": "Kubernetes", "limit": 5})
    assert not out.is_error

    # Output blocks separated by double newlines
    assert f"[runbook] Kubernetes Cluster Setup ({note1.id})" in out.content
    assert "Source: infra/k8s.md" in out.content
    assert f"[fact] Kubernetes Cluster Version ({note2.id})" in out.content
    assert "Source: none" in out.content

    # Check 500 chars truncation on long_text
    assert long_text[:500] in out.content
    assert long_text[:501] not in out.content


async def test_search_notes_shows_detail_line_when_full_text_only(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(type="fact", title="Python Version", text="Python 3.13 is used."),
    )

    tool = SearchNotes(knowledge_service)
    out = await tool.invoke(ctx, {"query": "Python"})
    assert not out.is_error
    assert "Note:" in out.content
    assert "embedder" in out.content.lower()


async def test_search_notes_no_matches(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SearchNotes(knowledge_service)
    out = await tool.invoke(ctx, {"query": "nonexistent term that matches nothing"})
    assert not out.is_error
    assert out.content.startswith("No notes match.")


async def test_search_notes_type_filtering(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(type="fact", title="Model Metric", text="Accuracy is 94% on test set."),
    )
    await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(
            type="runbook", title="Model Evaluation", text="Evaluate accuracy on test set."
        ),
    )

    tool = SearchNotes(knowledge_service)
    out = await tool.invoke(ctx, {"query": "accuracy", "type": "runbook"})
    assert not out.is_error
    assert "[runbook] Model Evaluation" in out.content
    assert "[fact] Model Metric" not in out.content


async def test_search_notes_limit_above_maximum_refused(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SearchNotes(knowledge_service)
    out = await tool.invoke(ctx, {"query": "test", "limit": 25})
    assert out.is_error
    assert "less than or equal to 20" in out.content


async def test_search_notes_output_capped_at_12000_with_omitted_line(ctx: ToolContext) -> None:
    # Build a fake knowledge object that returns 20 long notes
    now = datetime.now(UTC)
    notes: list[NoteView] = []
    for i in range(20):
        notes.append(
            NoteView(
                id=f"kno_long_{i:03d}",
                type="fact",
                title=f"A Very Long Note Title That Takes Some Space Number {i:03d}",
                text="A" * 600,  # Each note block text truncated to 500 chars
                source=f"https://example.com/source/{i:03d}",
                author="tester",
                created=now,
                updated=now,
            )
        )

    class FakeKnowledgeWithManyNotes:
        async def search(
            self, project_id: str, query: str, type: str | None, limit: int
        ) -> SearchResult:
            return SearchResult(notes=notes[:limit], used_vectors=True, detail=None)

    fake_kno = cast(KnowledgeService, FakeKnowledgeWithManyNotes())
    tool = SearchNotes(fake_kno)
    out = await tool.invoke(ctx, {"query": "test", "limit": 20})
    assert not out.is_error
    assert len(out.content) <= 12000
    assert "omitted due to length" in out.content


def test_search_notes_risk_and_policy() -> None:
    assert SearchNotes.risk == RiskClass.READ
    for level in cast(list[PermissionLevel], ["suggest", "ask", "autonomous", "bypass"]):
        assert decide(SearchNotes.risk, level) == "allow"
