import logging

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, field_validator

from mlops_agents.credentials import KeyStoreError
from mlops_agents.errors import DomainError


class AddThing(BaseModel):
    name: str
    api_key: str
    url: str

    @field_validator("url")
    @classmethod
    def https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Use https.")
        return value


async def test_domain_error_keeps_its_status_and_fields(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    async def fail() -> None:
        raise DomainError("x", "msg", status=409, detail={"a": 1})

    app.add_api_route("/api/test/domain", fail)

    response = await client.get("/api/test/domain")

    assert response.status_code == 409
    assert response.json() == {"code": "x", "message": "msg", "detail": {"a": 1}}


async def test_validation_error_names_the_field_without_the_submitted_values(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    async def add(body: AddThing) -> None:
        del body

    app.add_api_route("/api/test/things", add, methods=["POST"])

    response = await client.post(
        "/api/test/things", json={"api_key": "sk-fake-key-123", "url": "https://x"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["message"] == "Some fields are missing or invalid."
    assert [e["field"] for e in body["detail"]["errors"]] == ["body.name"]
    assert "sk-fake-key-123" not in response.text


async def test_validator_message_comes_back_without_the_value_error_prefix(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    async def add(body: AddThing) -> None:
        del body

    app.add_api_route("/api/test/things", add, methods=["POST"])

    response = await client.post(
        "/api/test/things", json={"name": "n", "api_key": "sk-fake-key-123", "url": "http://x"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["errors"] == [{"field": "body.url", "message": "Use https."}]
    assert "sk-fake-key-123" not in response.text


async def test_keystore_error_is_503_with_its_message(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    async def fail() -> None:
        raise KeyStoreError("Unlock your keyring.")

    app.add_api_route("/api/test/keystore", fail)

    response = await client.get("/api/test/keystore")

    assert response.status_code == 503
    assert response.json() == {
        "code": "keystore_unavailable",
        "message": "Unlock your keyring.",
        "detail": None,
    }


async def test_unknown_api_path_is_404_not_found(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["detail"] is None


async def test_unexpected_error_is_a_plain_500_without_its_text_or_traceback(
    app: FastAPI, client: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def fail() -> None:
        raise RuntimeError("secret sk-test-123")

    app.add_api_route("/api/test/crash", fail)

    response = await client.get("/api/test/crash")

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "Something went wrong on the server. The server log has the details.",
        "detail": None,
    }
    assert "sk-test-123" not in response.text
    assert all(record.exc_info is None for record in caplog.records)
    logged = [r for r in caplog.records if r.name == "mlops_agents.api.errors"]
    assert [r.getMessage() for r in logged] == [
        "Unexpected RuntimeError on GET /api/test/crash: secret sk-test-123"
    ]


async def test_unexpected_error_log_scrubs_secrets_in_urls(
    app: FastAPI, client: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def fail() -> None:
        raise RuntimeError("call to https://api.example.com/v1?key=sk-test-123 failed")

    app.add_api_route("/api/test/crash", fail)

    response = await client.get("/api/test/crash")

    assert response.status_code == 500
    assert not [r for r in caplog.records if "sk-test-123" in r.getMessage()]
