from __future__ import annotations

import httpx2

from tests.fakes.fake_openai import RunningFake


async def test_anonymous_cannot_access_models(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/models")).status_code == 401
    assert (await anonymous.post("/api/models", json={})).status_code == 401
    assert (await anonymous.delete("/api/models?address=fake:fake-tools")).status_code == 401
    assert (await anonymous.post("/api/models/verify", json={})).status_code == 401
    assert (await anonymous.put("/api/models/context-window", json={})).status_code == 401
    assert (await anonymous.put("/api/models/request-limit", json={})).status_code == 401


async def test_models_crud_verification_and_context_window(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
    # 0. Setup a self-hosted provider
    res = await client.post(
        "/api/providers",
        json={
            "name": "fake",
            "kind": "self_hosted",
            "base_url": fake_openai.base_url,
        },
    )
    assert res.status_code == 201

    # 1. Add fake-tools model -> 201
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-tools"},
    )
    assert res.status_code == 201, res.text
    tools_model = res.json()
    assert tools_model["address"] == "fake:fake-tools"
    assert tools_model["context_window"] == 32768
    assert tools_model["availability"] == "untested"
    assert tools_model["tool_calling"] is False
    assert tools_model["streaming"] is False

    # 2. Verify fake-tools -> availability verified, tool_calling=True, streaming=True
    res = await client.post(
        "/api/models/verify",
        json={"address": "fake:fake-tools"},
    )
    assert res.status_code == 200, res.text
    verified_tools = res.json()
    assert verified_tools["availability"] == "verified"
    assert verified_tools["tool_calling"] is True
    assert verified_tools["streaming"] is True
    assert verified_tools["verified_at"] is not None

    # 3. Add fake-no-tools model and verify -> tool_calling=False, streaming=True
    res = await client.post(
        "/api/models",
        json={"provider": "fake", "wire_model_id": "fake-no-tools"},
    )
    assert res.status_code == 201
    res = await client.post(
        "/api/models/verify",
        json={"address": "fake:fake-no-tools"},
    )
    assert res.status_code == 200, res.text
    verified_no_tools = res.json()
    assert verified_no_tools["availability"] == "verified"
    assert verified_no_tools["tool_calling"] is False
    assert verified_no_tools["streaming"] is True

    # 4. Set context window -> 200 with new value; invalid value <= 0 -> 422
    res = await client.put(
        "/api/models/context-window",
        json={"address": "fake:fake-tools", "value": 65536},
    )
    assert res.status_code == 200, res.text
    assert res.json()["context_window"] == 65536

    res = await client.put(
        "/api/models/context-window",
        json={"address": "fake:fake-tools", "value": 0},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_context_window"

    res = await client.put(
        "/api/models/context-window",
        json={"address": "fake:fake-tools", "value": -100},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_context_window"

    # 5. Add model with wire id containing '/' (slashes)
    res = await client.post(
        "/api/models",
        json={
            "provider": "fake",
            "wire_model_id": "meta-llama/llama-3.1-8b-instruct",
            "context_window": 131072,
        },
    )
    assert res.status_code == 201, res.text
    slash_model = res.json()
    assert slash_model["address"] == "fake:meta-llama/llama-3.1-8b-instruct"
    assert slash_model["wire_model_id"] == "meta-llama/llama-3.1-8b-instruct"
    assert slash_model["context_window"] == 131072

    # 6. Delete model via query param (?address=...)
    res = await client.delete(
        "/api/models",
        params={"address": "fake:meta-llama/llama-3.1-8b-instruct"},
    )
    assert res.status_code == 204

    # 7. Model is removed from list
    res = await client.get("/api/models")
    assert res.status_code == 200
    listed_addresses = [m["address"] for m in res.json()]
    assert "fake:meta-llama/llama-3.1-8b-instruct" not in listed_addresses
    assert "fake:fake-tools" in listed_addresses
    assert "fake:fake-no-tools" in listed_addresses


async def test_set_and_clear_request_limit(
    client: httpx2.AsyncClient,
    fake_openai: RunningFake,
) -> None:
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
    assert res.json().get("request_token_limit") is None

    # Set request limit -> 200 with new limit
    res = await client.put(
        "/api/models/request-limit",
        json={"address": "fake:fake-tools", "value": 8000},
    )
    assert res.status_code == 200, res.text
    assert res.json()["request_token_limit"] == 8000

    # Model in list also has request_token_limit
    res = await client.get("/api/models")
    assert res.status_code == 200
    model = next(m for m in res.json() if m["address"] == "fake:fake-tools")
    assert model["request_token_limit"] == 8000

    # Clear request limit with null -> 200 with request_token_limit is None
    res = await client.put(
        "/api/models/request-limit",
        json={"address": "fake:fake-tools", "value": None},
    )
    assert res.status_code == 200, res.text
    assert res.json()["request_token_limit"] is None

    # Invalid values: 0 -> 422 invalid_request_limit
    res = await client.put(
        "/api/models/request-limit",
        json={"address": "fake:fake-tools", "value": 0},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request_limit"

    # Invalid values: -50 -> 422 invalid_request_limit
    res = await client.put(
        "/api/models/request-limit",
        json={"address": "fake:fake-tools", "value": -50},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request_limit"

    # Unknown model -> 404
    res = await client.put(
        "/api/models/request-limit",
        json={"address": "fake:nonexistent", "value": 8000},
    )
    assert res.status_code == 404
