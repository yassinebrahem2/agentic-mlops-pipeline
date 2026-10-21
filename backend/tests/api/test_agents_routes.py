from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2

from tests.conftest import make_repo
from tests.fakes.fake_openai import RunningFake


async def test_anonymous_cannot_access_agents(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/agents")).status_code == 401
    assert (
        await anonymous.put(
            "/api/agents/orchestrator/model",
            json={"address": "fake:fake-tools"},
        )
    ).status_code == 401
    assert (await anonymous.get("/api/projects/prj_dummy/agents")).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/agents/orchestrator/model",
            json={"address": "fake:fake-tools"},
        )
    ).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/agents/orchestrator/limits",
            json={"max_steps": 5},
        )
    ).status_code == 401


async def test_agents_listing_and_default_model(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
    # 1. GET /api/agents initially lists orchestrator with chat: True, tools, and model_problem
    res = await client.get("/api/agents")
    assert res.status_code == 200, res.text
    agents: list[dict[str, Any]] = res.json()
    assert isinstance(agents, list)
    orchestrator = next((a for a in agents if a["name"] == "orchestrator"), None)
    assert orchestrator is not None
    assert orchestrator["display_name"] == "Orchestrator"
    assert orchestrator["chat"] is True
    assert set(orchestrator["tools"]) == {"Read", "Glob", "Grep"}
    assert orchestrator["settings"]["default_model"] is None
    assert orchestrator["settings"]["effective_model"] is None
    assert orchestrator["settings"]["model_problem"] is not None
    assert "Choose a model for Orchestrator" in orchestrator["settings"]["model_problem"]

    # 2. Register provider and a verified model
    res = await client.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake_openai.base_url},
    )
    assert res.status_code == 201
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-tools"},
    )
    assert res.status_code == 201
    res = await client.post(
        "/api/models/verify",
        json={"address": "fake:fake-tools"},
    )
    assert res.status_code == 200

    # 3. PUT /api/agents/orchestrator/model with verified model -> problem gone, effective_model set
    res = await client.put(
        "/api/agents/orchestrator/model",
        json={"address": "fake:fake-tools"},
    )
    assert res.status_code == 200, res.text
    info = res.json()
    assert info["name"] == "orchestrator"
    assert info["settings"]["default_model"] == "fake:fake-tools"
    assert info["settings"]["effective_model"] == "fake:fake-tools"
    assert info["settings"]["model_problem"] is None

    # Follow-up GET /api/agents confirms the update
    res = await client.get("/api/agents")
    assert res.status_code == 200
    agents_after: list[dict[str, Any]] = res.json()
    orchestrator_after = next(a for a in agents_after if a["name"] == "orchestrator")
    assert orchestrator_after["settings"]["default_model"] == "fake:fake-tools"
    assert orchestrator_after["settings"]["effective_model"] == "fake:fake-tools"
    assert orchestrator_after["settings"]["model_problem"] is None

    # 4. PUT /api/agents/orchestrator/model with address: None -> clears default model
    res = await client.put(
        "/api/agents/orchestrator/model",
        json={"address": None},
    )
    assert res.status_code == 200, res.text
    cleared = res.json()
    assert cleared["settings"]["default_model"] is None
    assert cleared["settings"]["effective_model"] is None
    assert cleared["settings"]["model_problem"] is not None


async def test_project_agent_model_and_limits(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    # Set up two providers pointing to fake_openai, each with a verified fake-tools model
    await client.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake_openai.base_url},
    )
    await client.post("/api/models", json={"provider": "fake", "wire_model_id": "fake-tools"})
    await client.post("/api/models/verify", json={"address": "fake:fake-tools"})

    await client.post(
        "/api/providers",
        json={"name": "fake2", "kind": "self_hosted", "base_url": fake_openai.base_url},
    )
    await client.post("/api/models", json={"provider": "fake2", "wire_model_id": "fake-tools"})
    await client.post("/api/models/verify", json={"address": "fake2:fake-tools"})

    # Set default model for orchestrator
    await client.put(
        "/api/agents/orchestrator/model",
        json={"address": "fake:fake-tools"},
    )

    # Create a project
    repo = make_repo(tmp_path / "project", {"README.md": "# Project\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    # Initial project agent view inherits default model and definition limits
    res = await client.get(f"/api/projects/{project_id}/agents")
    assert res.status_code == 200, res.text
    proj_agents: list[dict[str, Any]] = res.json()
    orch = next(a for a in proj_agents if a["name"] == "orchestrator")
    assert orch["settings"]["default_model"] == "fake:fake-tools"
    assert orch["settings"]["project_model"] is None
    assert orch["settings"]["effective_model"] == "fake:fake-tools"
    assert orch["settings"]["max_steps"] == 25

    # Override model for this project
    res = await client.put(
        f"/api/projects/{project_id}/agents/orchestrator/model",
        json={"address": "fake2:fake-tools"},
    )
    assert res.status_code == 200, res.text
    proj_info = res.json()
    assert proj_info["settings"]["project_model"] == "fake2:fake-tools"
    assert proj_info["settings"]["effective_model"] == "fake2:fake-tools"
    assert proj_info["settings"]["default_model"] == "fake:fake-tools"

    # App-level model is unaffected
    res = await client.get("/api/agents")
    assert res.status_code == 200
    app_agents: list[dict[str, Any]] = res.json()
    app_orch = next(a for a in app_agents if a["name"] == "orchestrator")
    assert app_orch["settings"]["default_model"] == "fake:fake-tools"
    assert app_orch["settings"]["project_model"] is None
    assert app_orch["settings"]["effective_model"] == "fake:fake-tools"

    # Override limits: max_steps: 5
    res = await client.put(
        f"/api/projects/{project_id}/agents/orchestrator/limits",
        json={"max_steps": 5},
    )
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["max_steps"] == 5

    # Override limits: max_steps: null (no limit)
    res = await client.put(
        f"/api/projects/{project_id}/agents/orchestrator/limits",
        json={"max_steps": None},
    )
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["max_steps"] is None

    # Invalid max_steps: 0 -> 422
    res = await client.put(
        f"/api/projects/{project_id}/agents/orchestrator/limits",
        json={"max_steps": 0},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"


async def test_agents_errors_and_validation(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    # Set up provider and an unverified model
    await client.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake_openai.base_url},
    )
    await client.post("/api/models", json={"provider": "fake", "wire_model_id": "unverified"})

    # Setting unverified model at app level -> 409 model_not_usable
    res = await client.put(
        "/api/agents/orchestrator/model",
        json={"address": "fake:unverified"},
    )
    assert res.status_code == 409
    assert res.json()["code"] == "model_not_usable"

    # Unknown agent at app level -> 404 agent_not_found
    res = await client.put(
        "/api/agents/unknown-agent/model",
        json={"address": None},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "agent_not_found"

    # Unknown project -> 404 project_not_found
    res = await client.get("/api/projects/prj_0000000000000000/agents")
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"

    res = await client.put(
        "/api/projects/prj_0000000000000000/agents/orchestrator/model",
        json={"address": None},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"

    res = await client.put(
        "/api/projects/prj_0000000000000000/agents/orchestrator/limits",
        json={"max_steps": 5},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"

    # Project exists, but unknown agent -> 404 agent_not_found
    repo = make_repo(tmp_path / "project", {"README.md": "# Project\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    res = await client.put(
        f"/api/projects/{project_id}/agents/unknown-agent/model",
        json={"address": None},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "agent_not_found"

    res = await client.put(
        f"/api/projects/{project_id}/agents/unknown-agent/limits",
        json={"max_steps": 5},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "agent_not_found"

    # Extra fields rejected (extra="forbid") -> 422 invalid_request
    res = await client.put(
        "/api/agents/orchestrator/model",
        json={"address": None, "unexpected": "value"},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
