from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.policy.table import PermissionLevel, decide
from mlops_agents.agents.tools.base import RiskClass, ToolContext
from mlops_agents.agents.tools.save_note import SaveNote
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.knowledge.contract import NoteDraft
from mlops_agents.knowledge.limits import TEXT_MAX_CHARS, TITLE_MAX_CHARS
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


async def test_save_note_creates_note(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SaveNote(knowledge_service)
    out = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "Model Architecture",
            "text": "Uses ResNet-50 backbone with custom classification head.",
            "source": "docs/architecture.md",
        },
    )
    assert not out.is_error
    assert out.content.startswith("Saved note kno_")
    assert "(fact): Model Architecture" in out.content

    # Extract note id from output
    note_id = out.content.split()[2]
    saved = await knowledge_service.get(ctx.project_id, note_id)
    assert saved is not None
    assert saved.id == note_id
    assert saved.author == ctx.agent
    assert saved.type == "fact"
    assert saved.title == "Model Architecture"
    assert saved.text == "Uses ResNet-50 backbone with custom classification head."
    assert saved.source == "docs/architecture.md"


async def test_save_note_updates_existing_note(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    initial = await knowledge_service.save(
        ctx.project_id,
        author=ctx.agent,
        note=NoteDraft(type="decision", title="Initial Decision", text="Initial rationale."),
    )

    tool = SaveNote(knowledge_service)
    out = await tool.invoke(
        ctx,
        {
            "note_id": initial.id,
            "type": "decision",
            "title": "Updated Decision",
            "text": "Updated rationale with benchmark evidence.",
        },
    )
    assert not out.is_error
    assert out.content.startswith(f"Updated note {initial.id}")
    assert "Updated Decision" in out.content

    updated = await knowledge_service.get(ctx.project_id, initial.id)
    assert updated is not None
    assert updated.id == initial.id
    assert updated.author == ctx.agent
    assert updated.title == "Updated Decision"
    assert updated.text == "Updated rationale with benchmark evidence."


async def test_save_note_rejects_extra_fields(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SaveNote(knowledge_service)
    out = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "Extra Fields Note",
            "text": "Some text",
            "unknown_extra": "forbidden",
        },
    )
    assert out.is_error
    assert "extra" in out.content.lower()


async def test_save_note_rejects_invalid_type(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SaveNote(knowledge_service)
    out = await tool.invoke(
        ctx,
        {
            "type": "invalid_type",
            "title": "Title",
            "text": "Some text",
        },
    )
    assert out.is_error
    assert "type" in out.content.lower()


async def test_save_note_empty_title_or_text_returns_tool_error(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SaveNote(knowledge_service)
    out_empty_title = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "   ",
            "text": "Valid text",
        },
    )
    assert out_empty_title.is_error
    assert "empty" in out_empty_title.content.lower()

    out_empty_text = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "Valid title",
            "text": "",
        },
    )
    assert out_empty_text.is_error
    assert "empty" in out_empty_text.content.lower()


async def test_save_note_overlong_text_returns_cap_in_error(
    ctx: ToolContext, knowledge_service: KnowledgeService
) -> None:
    tool = SaveNote(knowledge_service)
    out_long_title = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "x" * (TITLE_MAX_CHARS + 1),
            "text": "Valid text",
        },
    )
    assert out_long_title.is_error
    assert str(TITLE_MAX_CHARS) in out_long_title.content

    out_long_text = await tool.invoke(
        ctx,
        {
            "type": "fact",
            "title": "Valid title",
            "text": "x" * (TEXT_MAX_CHARS + 1),
        },
    )
    assert out_long_text.is_error
    assert str(TEXT_MAX_CHARS) in out_long_text.content


def test_save_note_risk_and_policy() -> None:
    assert SaveNote.risk == RiskClass.READ
    for level in cast(list[PermissionLevel], ["suggest", "ask", "autonomous", "bypass"]):
        assert decide(SaveNote.risk, level) == "allow"


def test_default_registry_registers_knowledge_tools(knowledge_service: KnowledgeService) -> None:
    from mlops_agents.agents.tools import default_registry

    reg_without = default_registry()
    assert "SaveNote" not in reg_without.names()
    assert "SearchNotes" not in reg_without.names()

    reg_with = default_registry(knowledge=knowledge_service)
    assert "SaveNote" in reg_with.names()
    assert "SearchNotes" in reg_with.names()
