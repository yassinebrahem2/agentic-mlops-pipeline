from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
from fastapi import FastAPI

from mlops_agents.api.openapi_export import export_text
from tests.api.conftest import add_local_project
from tests.fakes.fake_openai import RunningFake


async def test_anonymous_cannot_access_embedder_settings(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/settings/embedder")).status_code == 401
    assert (
        await anonymous.put("/api/settings/embedder", json={"address": "fake:fake-embed"})
    ).status_code == 401
    assert (await anonymous.get("/api/projects/prj_dummy/settings/embedder")).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/settings/embedder", json={"address": "fake:fake-embed"}
        )
    ).status_code == 401


async def _setup_provider_and_models(
    client: httpx2.AsyncClient, fake_openai: RunningFake
) -> tuple[str, str, str]:
    """Returns (embedder_address, wide_embedder_address, chat_address)."""
    res = await client.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake_openai.base_url},
    )
    assert res.status_code == 201

    # 1. Verified 16-dim embedder
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-embed", "model_type": "embedder"},
    )
    assert res.status_code == 201
    embed_addr = res.json()["address"]
    res = await client.post("/api/models/verify", json={"address": embed_addr})
    assert res.status_code == 200

    # 2. Verified 32-dim embedder
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-embed-wide", "model_type": "embedder"},
    )
    assert res.status_code == 201
    wide_addr = res.json()["address"]
    res = await client.post("/api/models/verify", json={"address": wide_addr})
    assert res.status_code == 200

    # 3. Verified chat model
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-tools", "model_type": "chat"},
    )
    assert res.status_code == 201
    chat_addr = res.json()["address"]
    res = await client.post("/api/models/verify", json={"address": chat_addr})
    assert res.status_code == 200

    return embed_addr, wide_addr, chat_addr


async def test_default_embedder_lifecycle(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
    embed_addr, _, _ = await _setup_provider_and_models(client, fake_openai)

    # Initial state: unconfigured
    res = await client.get("/api/settings/embedder")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": False,
            "address": None,
            "dimensions": None,
            "problem": None,
        },
    }

    # Set verified embedder
    res = await client.put(
        "/api/settings/embedder",
        json={"address": embed_addr},
    )
    assert res.status_code == 200, res.text
    expected_configured = {
        "chosen": embed_addr,
        "status": {
            "configured": True,
            "address": embed_addr,
            "dimensions": 16,
            "problem": None,
        },
    }
    assert res.json() == expected_configured

    # Read back
    res = await client.get("/api/settings/embedder")
    assert res.status_code == 200
    assert res.json() == expected_configured

    # Clear default embedder
    res = await client.put(
        "/api/settings/embedder",
        json={"address": None},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": False,
            "address": None,
            "dimensions": None,
            "problem": None,
        },
    }

    # Verify cleared on GET
    res = await client.get("/api/settings/embedder")
    assert res.status_code == 200
    assert res.json()["chosen"] is None
    assert res.json()["status"]["configured"] is False


async def test_embedder_refused_for_chat_unverified_and_unknown(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
    embed_addr, _, chat_addr = await _setup_provider_and_models(client, fake_openai)

    # Add unverified embedder
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "unverified-embed", "model_type": "embedder"},
    )
    assert res.status_code == 201
    unverified_addr = res.json()["address"]

    # 1. Refused for chat model -> 409 model_not_usable
    res = await client.put("/api/settings/embedder", json={"address": chat_addr})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "model_not_usable"
    assert "chat model" in body["message"]

    # 2. Refused for unverified embedder -> 409 model_not_usable
    res = await client.put("/api/settings/embedder", json={"address": unverified_addr})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "model_not_usable"
    assert "has not passed verification" in body["message"]

    # 3. Refused for unknown address -> 404 model_not_found
    res = await client.put("/api/settings/embedder", json={"address": "fake:does-not-exist"})
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "model_not_found"
    assert "fake:does-not-exist" in body["message"]

    # 4. Extra fields forbidden -> 422
    res = await client.put(
        "/api/settings/embedder",
        json={"address": embed_addr, "unexpected_field": True},
    )
    assert res.status_code == 422


async def test_project_embedder_lifecycle_isolation_and_fallback(
    app: FastAPI,
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    embed_addr, wide_addr, chat_addr = await _setup_provider_and_models(client, fake_openai)
    p1 = await add_local_project(app, tmp_path / "p1", git_env)
    p2 = await add_local_project(app, tmp_path / "p2", git_env)

    # 1. No app default, project has no override
    res = await client.get(f"/api/projects/{p1}/settings/embedder")
    assert res.status_code == 200
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": False,
            "address": None,
            "dimensions": None,
            "problem": None,
        },
    }

    # 2. Set app default
    res = await client.put("/api/settings/embedder", json={"address": embed_addr})
    assert res.status_code == 200

    # Project without override reports chosen null, and app default in status
    res = await client.get(f"/api/projects/{p1}/settings/embedder")
    assert res.status_code == 200
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": True,
            "address": embed_addr,
            "dimensions": 16,
            "problem": None,
        },
    }

    # 3. Set project override on p1 to wide embedder
    res = await client.put(
        f"/api/projects/{p1}/settings/embedder",
        json={"address": wide_addr},
    )
    assert res.status_code == 200
    assert res.json() == {
        "chosen": wide_addr,
        "status": {
            "configured": True,
            "address": wide_addr,
            "dimensions": 32,
            "problem": None,
        },
    }

    # p1 GET returns override as chosen
    res = await client.get(f"/api/projects/{p1}/settings/embedder")
    assert res.status_code == 200
    assert res.json()["chosen"] == wide_addr
    assert res.json()["status"]["address"] == wide_addr
    assert res.json()["status"]["dimensions"] == 32

    # 4. Project 2 isolation: p2 still reports chosen null and app default in status
    res = await client.get(f"/api/projects/{p2}/settings/embedder")
    assert res.status_code == 200
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": True,
            "address": embed_addr,
            "dimensions": 16,
            "problem": None,
        },
    }

    # 5. Clear project override on p1
    res = await client.put(
        f"/api/projects/{p1}/settings/embedder",
        json={"address": None},
    )
    assert res.status_code == 200
    assert res.json() == {
        "chosen": None,
        "status": {
            "configured": True,
            "address": embed_addr,
            "dimensions": 16,
            "problem": None,
        },
    }

    # 6. Project route validation errors
    # Chat model refused
    res = await client.put(f"/api/projects/{p1}/settings/embedder", json={"address": chat_addr})
    assert res.status_code == 409
    assert res.json()["code"] == "model_not_usable"

    # Unknown model refused
    res = await client.put(
        f"/api/projects/{p1}/settings/embedder", json={"address": "fake:not-found"}
    )
    assert res.status_code == 404
    assert res.json()["code"] == "model_not_found"

    # Unknown project -> 404
    res = await client.get("/api/projects/prj_unknown/settings/embedder")
    assert res.status_code == 404
    res = await client.put(
        "/api/projects/prj_unknown/settings/embedder", json={"address": embed_addr}
    )
    assert res.status_code == 404


async def test_deleting_chosen_model_leaves_chosen_and_fills_problem(
    app: FastAPI,
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    embed_addr, wide_addr, _ = await _setup_provider_and_models(client, fake_openai)
    project_id = await add_local_project(app, tmp_path / "prj", git_env)

    # Set app default and project override
    res = await client.put("/api/settings/embedder", json={"address": embed_addr})
    assert res.status_code == 200
    res = await client.put(
        f"/api/projects/{project_id}/settings/embedder", json={"address": wide_addr}
    )
    assert res.status_code == 200

    # Delete default model
    res = await client.delete("/api/models", params={"address": embed_addr})
    assert res.status_code == 204

    # App settings: chosen is still embed_addr, status.problem filled, dimensions None
    res = await client.get("/api/settings/embedder")
    assert res.status_code == 200
    data = res.json()
    assert data["chosen"] == embed_addr
    assert data["status"]["configured"] is True
    assert data["status"]["address"] == embed_addr
    assert data["status"]["dimensions"] is None
    assert data["status"]["problem"] is not None
    assert "no longer exists" in data["status"]["problem"]

    # Delete project override model
    res = await client.delete("/api/models", params={"address": wide_addr})
    assert res.status_code == 204

    # Project settings: chosen is still wide_addr, status.problem filled, dimensions None
    res = await client.get(f"/api/projects/{project_id}/settings/embedder")
    assert res.status_code == 200
    pdata = res.json()
    assert pdata["chosen"] == wide_addr
    assert pdata["status"]["configured"] is True
    assert pdata["status"]["address"] == wide_addr
    assert pdata["status"]["dimensions"] is None
    assert pdata["status"]["problem"] is not None
    assert "no longer exists" in pdata["status"]["problem"]


def test_openapi_spec_has_embedder_operations() -> None:
    spec = json.loads(export_text())
    paths: dict[str, Any] = spec.get("paths", {})
    assert "/api/settings/embedder" in paths
    assert "get" in paths["/api/settings/embedder"]
    assert "put" in paths["/api/settings/embedder"]

    assert "/api/projects/{project_id}/settings/embedder" in paths
    assert "get" in paths["/api/projects/{project_id}/settings/embedder"]
    assert "put" in paths["/api/projects/{project_id}/settings/embedder"]

    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    assert "EmbedderChoice" in schemas
    assert "EmbedderSettingView" in schemas
    assert "EmbedderStatus" in schemas
