"""Tests verifying build_services wiring for knowledge (Task 5)."""

from __future__ import annotations

from fastapi import FastAPI

from mlops_agents.knowledge.service import KnowledgeService


def test_build_services_knowledge_wiring(app: FastAPI) -> None:
    services = app.state.services
    assert isinstance(services.knowledge, KnowledgeService)

    # Verify tools registered in default registry
    tool_names = services.tools.names()
    assert "SaveNote" in tool_names
    assert "SearchNotes" in tool_names

    # Verify KnowledgeHook registered in hook registry
    assert services.sessions._builder._hooks is not None
    hook_names = services.sessions._builder._hooks.names()
    assert "knowledge" in hook_names
