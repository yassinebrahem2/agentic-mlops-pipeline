"""End-of-plan smoke test for knowledge (Task 5).

Verifies GET /notes, SaveNote tool, SearchNotes tool, and KnowledgeHook
against one running app with and without embedder.
"""

from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.knowledge import KnowledgeHook
from mlops_agents.agents.tools.save_note import SaveNote, SaveNoteArgs
from mlops_agents.agents.tools.search_notes import SearchNotes, SearchNotesArgs
from tests.agents.ctx import tool_context
from tests.agents.tools.test_save_note import DummyFiles
from tests.api.conftest import add_local_project
from tests.fakes.fake_openai import RunningFake


async def test_smoke_knowledge_without_embedder(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    knowledge = services.knowledge

    save_tool = SaveNote(knowledge)
    search_tool = SearchNotes(knowledge)
    hook = KnowledgeHook(knowledge)
    ctx = tool_context(DummyFiles(), project_id=project_id, agent="dev_agent")
    hook_ctx = HookContext(project_id=project_id, agent="dev_agent", trigger_text="postgres")

    # 1. SaveNote tool
    save_res = await save_tool.run(
        ctx,
        SaveNoteArgs(
            type="fact",
            title="Postgres Connection Tuning",
            text="Shared buffers set to 25% of total memory.",
        ),
    )
    assert "Saved note kno_" in save_res.content

    # 2. SearchNotes tool (without embedder -> words only, note detail present)
    search_res = await search_tool.run(
        ctx,
        SearchNotesArgs(query="postgres"),
    )
    assert "Postgres Connection Tuning" in search_res.content
    assert "Note: No embedder is set" in search_res.content

    # 3. KnowledgeHook
    hook_res = await hook.render(hook_ctx)
    assert hook_res.startswith("Project knowledge:")
    assert "Postgres Connection Tuning" in hook_res

    # 4. Route GET /api/projects/{id}/notes
    api_res = await client.get(f"/api/projects/{project_id}/notes?q=postgres")
    assert api_res.status_code == 200
    data = api_res.json()
    assert data["used_vectors"] is False
    assert data["detail"] is not None
    assert "No embedder is set" in data["detail"]
    assert len(data["notes"]) == 1
    assert data["notes"][0]["title"] == "Postgres Connection Tuning"


async def test_smoke_knowledge_with_fake_embedder(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    fake_openai: RunningFake,
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    knowledge = services.knowledge

    from mlops_agents.providers.models import ModelCreate, ProviderCreate

    # Register fake provider and embedder model
    await services.providers.create_provider(
        ProviderCreate(name="smoke-prov", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await services.providers.add_model(
        ModelCreate(provider="smoke-prov", wire_model_id="fake-embed", model_type="embedder")
    )
    await services.providers.verify_model("smoke-prov:fake-embed")

    # Set as project embedder override
    emb_res = await client.put(
        f"/api/projects/{project_id}/settings/embedder",
        json={"address": "smoke-prov:fake-embed"},
    )
    assert emb_res.status_code == 200

    save_tool = SaveNote(knowledge)
    search_tool = SearchNotes(knowledge)
    hook = KnowledgeHook(knowledge)
    ctx = tool_context(DummyFiles(), project_id=project_id, agent="dev_agent")
    hook_ctx = HookContext(project_id=project_id, agent="dev_agent", trigger_text="neural")

    # 1. SaveNote tool
    save_res = await save_tool.run(
        ctx,
        SaveNoteArgs(
            type="decision",
            title="Neural Architecture Choice",
            text="Transformer models used for all text embeddings.",
        ),
    )
    assert "Saved note kno_" in save_res.content

    # Wait for re-embedding pass to finish
    await knowledge.wait_idle(project_id)

    # 2. Route GET /api/projects/{id}/notes uses vectors
    api_res = await client.get(f"/api/projects/{project_id}/notes?q=neural")
    assert api_res.status_code == 200
    data = api_res.json()
    assert data["used_vectors"] is True
    assert data["detail"] is None
    assert len(data["notes"]) == 1
    assert data["notes"][0]["title"] == "Neural Architecture Choice"

    # 3. SearchNotes tool uses vectors (no fallback Note: line)
    search_res = await search_tool.run(
        ctx,
        SearchNotesArgs(query="neural"),
    )
    assert "Neural Architecture Choice" in search_res.content
    assert "Note: " not in search_res.content

    # 4. KnowledgeHook
    hook_res = await hook.render(hook_ctx)
    assert hook_res.startswith("Project knowledge:")
    assert "Neural Architecture Choice" in hook_res
