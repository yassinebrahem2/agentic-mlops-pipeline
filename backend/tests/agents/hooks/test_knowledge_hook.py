from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy.exc import SQLAlchemyError

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.knowledge import KnowledgeHook
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.errors import DomainError
from mlops_agents.knowledge.contract import Knowledge, NoteView, SearchResult
from tests.conftest import STAGE_2_TOOLS


class FakeKnowledge:
    def __init__(self, result: SearchResult | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    async def search(
        self, project_id: str, query: str, type: str | None, limit: int
    ) -> SearchResult:
        self.calls.append({"project_id": project_id, "query": query, "type": type, "limit": limit})
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


CTX = HookContext(project_id="prj_1", agent="orchestrator", trigger_text="how to deploy")


def _make_note(idx: int, text: str = "A" * 400) -> NoteView:
    now = datetime.now(UTC)
    return NoteView(
        id=f"kno_{idx:03d}",
        type="fact",
        title=f"Deployment Note {idx}",
        text=text,
        source="infra/k8s.md",
        author="tester",
        created=now,
        updated=now,
    )


def test_knowledge_hook_name() -> None:
    hook = KnowledgeHook(cast(Knowledge, FakeKnowledge()))
    assert hook.name == "knowledge"


async def test_knowledge_hook_blank_trigger_returns_empty() -> None:
    fake = FakeKnowledge(SearchResult(notes=[_make_note(1)], used_vectors=True, detail=None))
    hook = KnowledgeHook(cast(Knowledge, fake))

    assert (
        await hook.render(HookContext(project_id="prj_1", agent="orchestrator", trigger_text=""))
        == ""
    )
    assert (
        await hook.render(
            HookContext(project_id="prj_1", agent="orchestrator", trigger_text="   \n\t ")
        )
        == ""
    )
    assert fake.calls == []


async def test_knowledge_hook_no_notes_returns_empty() -> None:
    fake = FakeKnowledge(SearchResult(notes=[], used_vectors=False, detail="detail"))
    hook = KnowledgeHook(cast(Knowledge, fake))

    out = await hook.render(CTX)
    assert out == ""
    assert len(fake.calls) == 1
    assert fake.calls[0]["limit"] == 5


async def test_knowledge_hook_renders_five_best_notes_formatted() -> None:
    notes = [_make_note(i, text=f"Note content {i} " + "X" * 350) for i in range(1, 7)]
    fake = FakeKnowledge(SearchResult(notes=notes[:5], used_vectors=True, detail=None))
    hook = KnowledgeHook(cast(Knowledge, fake))

    out = await hook.render(CTX)
    lines = out.splitlines()
    assert lines[0] == "Project knowledge:"
    assert len(lines) == 6  # heading + 5 notes

    # Check formatting of first note: text cut at 300 characters
    expected_note1 = f"- [fact] Deployment Note 1 (kno_001): {notes[0].text[:300]}"
    assert lines[1] == expected_note1
    assert notes[0].text[:301] not in lines[1]


async def test_knowledge_hook_capped_at_2500_chars() -> None:
    # 5 notes with huge titles / texts
    now = datetime.now(UTC)
    notes = [
        NoteView(
            id=f"kno_{i:03d}",
            type="fact",
            title=f"Very Long Title {'T' * 150} {i}",
            text="W" * 500,
            source=None,
            author="tester",
            created=now,
            updated=now,
        )
        for i in range(5)
    ]
    fake = FakeKnowledge(SearchResult(notes=notes, used_vectors=True, detail=None))
    hook = KnowledgeHook(cast(Knowledge, fake))

    out = await hook.render(CTX)
    assert len(out) <= 2500
    assert out.startswith("Project knowledge:")


async def test_knowledge_hook_never_prints_search_detail() -> None:
    detail_marker = "sensitive-embedder-failure-marker-999"
    fake = FakeKnowledge(
        SearchResult(notes=[_make_note(1, "normal text")], used_vectors=False, detail=detail_marker)
    )
    hook = KnowledgeHook(cast(Knowledge, fake))

    out = await hook.render(CTX)
    assert detail_marker not in out
    assert "Project knowledge:" in out


async def test_knowledge_hook_error_handling() -> None:
    # DomainError
    fake_domain = FakeKnowledge(exc=DomainError("locked", "Database is locked"))
    hook_domain = KnowledgeHook(cast(Knowledge, fake_domain))
    out_domain = await hook_domain.render(CTX)
    assert out_domain == "Knowledge notes are unavailable: Database is locked"

    # SQLAlchemyError
    fake_sql = FakeKnowledge(exc=SQLAlchemyError("Connection refused"))
    hook_sql = KnowledgeHook(cast(Knowledge, fake_sql))
    out_sql = await hook_sql.render(CTX)
    assert out_sql == "Knowledge notes are unavailable: Connection refused"


def test_knowledge_hook_catalog_load(tmp_path: Path) -> None:
    registry = HookRegistry()
    registry.register(KnowledgeHook(cast(Knowledge, FakeKnowledge())))

    (tmp_path / "knowledge_agent.md").write_text(
        """---
name: knowledge-agent
display_name: Knowledge Agent
description: Tests knowledge hook loading
tools: [Read]
chat: true
schedules: false
can_message: []
context_hooks: [knowledge]
status_board: false
limits:
  max_steps: 10
---
System prompt.
""",
        encoding="utf-8",
    )

    catalog = AgentCatalog.load(STAGE_2_TOOLS, [tmp_path], hook_names=registry.names())
    agent = catalog.get("knowledge-agent")
    assert agent.hook_names == ("knowledge",)
