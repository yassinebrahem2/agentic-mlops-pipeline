from __future__ import annotations

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.providers.service import ProvidersService
from tests.fakes.fake_openai import RunningFake


async def test_anonymous_cannot_access_providers(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/providers")).status_code == 401
    assert (await anonymous.post("/api/providers", json={})).status_code == 401
    assert (await anonymous.delete("/api/providers/fake")).status_code == 401
    assert (await anonymous.post("/api/providers/fake/keys", json={})).status_code == 401
    assert (await anonymous.delete("/api/providers/fake/keys/1")).status_code == 401
    assert (await anonymous.get("/api/providers/fake/remote-models")).status_code == 401
    assert (await anonymous.get("/api/env-keys")).status_code == 401
    assert (await anonymous.post("/api/env-keys/GROQ_API_KEY/import")).status_code == 401


async def test_providers_crud_and_keys(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
    # 1. Initially empty providers list
    res = await client.get("/api/providers")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Base URL with credentials is rejected with 422, validator message, and credentials scrubbed
    secret_cred = "secret_password_never_leak"
    res = await client.post(
        "/api/providers",
        json={
            "name": "bad-creds",
            "kind": "self_hosted",
            "base_url": f"https://user:{secret_cred}@example.com/v1",
        },
    )
    assert res.status_code == 422
    assert "must not carry credentials" in res.text
    assert secret_cred not in res.text

    # 3. Create self-hosted provider with fake's base URL -> 201
    res = await client.post(
        "/api/providers",
        json={
            "name": "fake",
            "kind": "self_hosted",
            "base_url": fake_openai.base_url,
        },
    )
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["id"].startswith("prv_")
    assert created["name"] == "fake"
    assert created["kind"] == "self_hosted"
    assert created["base_url"] == fake_openai.base_url
    assert created["status"] == "ok"
    assert created["keys"] == []

    # 4. Duplicate name -> 409
    res = await client.post(
        "/api/providers",
        json={
            "name": "fake",
            "kind": "self_hosted",
            "base_url": fake_openai.base_url,
        },
    )
    assert res.status_code == 409

    # 5. Add a key -> returns Provider with masked hint only; raw key never appears in response body
    secret_key = "sk-fake-secret-test-key-987654"
    res = await client.post(
        "/api/providers/fake/keys",
        json={"key": secret_key},
    )
    assert res.status_code == 200, res.text
    provider_with_key = res.json()
    assert len(provider_with_key["keys"]) == 1
    assert provider_with_key["keys"][0]["position"] == 1
    assert provider_with_key["keys"][0]["hint"] == "••••7654"
    assert secret_key not in res.text

    # Search every response to ensure secret_key never leaks
    list_res = await client.get("/api/providers")
    assert secret_key not in list_res.text

    # 6. Remote models lists fake's models with their context windows
    res = await client.get("/api/providers/fake/remote-models")
    assert res.status_code == 200
    assert secret_key not in res.text
    remote_models = res.json()
    model_ids = {m["id"]: m["context_window"] for m in remote_models}
    assert "fake-tools" in model_ids
    assert model_ids["fake-tools"] == 32768
    assert "fake-no-tools" in model_ids
    assert model_ids["fake-no-tools"] == 8192

    # 7. Remove key by position
    res = await client.delete("/api/providers/fake/keys/1")
    assert res.status_code == 200
    assert secret_key not in res.text
    assert res.json()["keys"] == []

    # 8. Delete provider -> 204
    res = await client.delete("/api/providers/fake")
    assert res.status_code == 204
    assert secret_key not in res.text

    # Final GET: provider is gone and secret_key nowhere in response
    res = await client.get("/api/providers")
    assert res.status_code == 200
    assert res.json() == []
    assert secret_key not in res.text


async def test_env_keys_detection_and_import(
    client: httpx2.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_key = "gsk_test_groq_api_key_4321"
    monkeypatch.setenv("GROQ_API_KEY", test_key)

    # 1. GET /api/env-keys contains GROQ_API_KEY masked
    res = await client.get("/api/env-keys")
    assert res.status_code == 200
    detected = res.json()
    assert test_key not in res.text
    matching = [d for d in detected if d["env_var"] == "GROQ_API_KEY"]
    assert len(matching) == 1
    groq_detected = matching[0]
    assert groq_detected["provider_name"] == "groq"
    assert groq_detected["base_url"] == "https://api.groq.com/openai/v1"
    assert groq_detected["hint"] == "••••4321"
    assert groq_detected["provider_exists"] is False

    # 2. Mock client so import does not hit real Groq API
    def groq_handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).startswith("https://api.groq.com/openai/v1")
        assert request.url.path.endswith("/models")
        if request.headers.get("authorization") == f"Bearer {test_key}":
            return httpx2.Response(
                200,
                json={"object": "list", "data": [{"id": "llama-3.1-8b-instant"}]},
            )
        return httpx2.Response(401, json={"error": {"message": "Invalid API key"}})

    mock_client = httpx2.AsyncClient(transport=httpx2.MockTransport(groq_handler))
    try:
        services = app.state.services
        app.state.services.providers = ProvidersService(
            services.databases, services.keystore, mock_client, services.failover
        )

        # 3. POST /api/env-keys/GROQ_API_KEY/import -> 200 Provider
        res = await client.post("/api/env-keys/GROQ_API_KEY/import")
        assert res.status_code == 200, res.text
        imported = res.json()
        assert imported["name"] == "groq"
        assert imported["kind"] == "hosted"
        assert len(imported["keys"]) == 1
        assert imported["keys"][0]["hint"] == "••••4321"
        assert test_key not in res.text

        # Provider now exists in list
        res = await client.get("/api/providers")
        assert res.status_code == 200
        assert any(p["name"] == "groq" for p in res.json())
        assert test_key not in res.text
    finally:
        await mock_client.aclose()

