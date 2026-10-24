from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest

from mlops_agents.providers.listing import ListedModel, match_model_id
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore

GEMINI_KEY = "AIzaSyTestGeminiKey12345"
VLLM_KEY = "vllm_test_key_67890"

Handler = Callable[[httpx2.Request], httpx2.Response]
MakeService = Callable[..., ProvidersService]


@pytest.fixture
async def make_service(
    databases: Databases, keystore: MemoryKeyStore
) -> AsyncIterator[MakeService]:
    clients: list[httpx2.AsyncClient] = []

    def make(handler: Handler) -> ProvidersService:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        clients.append(client)
        return ProvidersService(databases, keystore, client)

    yield make
    for client in clients:
        await client.aclose()


def test_listed_model_context_fields() -> None:
    # Existing fields
    assert ListedModel(id="m", context_window=8192).context() == 8192
    assert ListedModel(id="m", context_length=4096).context() == 4096
    assert ListedModel(id="m", max_context_length=16384).context() == 16384
    # vLLM max_model_len
    assert ListedModel(id="m", max_model_len=32768).context() == 32768
    # Invalid values
    assert ListedModel(id="m", max_model_len="big").context() is None
    assert ListedModel(id="m", max_model_len=True).context() is None
    assert ListedModel(id="m", max_model_len=-100).context() is None
    assert ListedModel(id="m", max_model_len=0).context() is None
    assert ListedModel(id="m").context() is None


def test_match_model_id() -> None:
    assert match_model_id("gemini-1.5-pro", "gemini-1.5-pro")
    assert match_model_id("models/gemini-1.5-pro", "gemini-1.5-pro")
    assert match_model_id("gemini-1.5-pro", "models/gemini-1.5-pro")
    assert match_model_id("models/gemini-1.5-pro", "models/gemini-1.5-pro")
    assert match_model_id("  models/gemini-1.5-pro  ", "gemini-1.5-pro")
    assert not match_model_id("models/gemini-1.5-pro", "gemini-1.5-flash")
    assert not match_model_id("gemini-1.5-pro", "models/gemini-1.5-flash")


async def test_gemini_shaped_provider_reads_native_listing(make_service: MakeService) -> None:
    seen_requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        url_str = str(request.url)

        # 1. Validation call on add_key or listing call to OpenAI /models
        if request.url.path == "/v1beta/openai/models":
            assert request.headers.get("authorization") == f"Bearer {GEMINI_KEY}"
            assert "x-goog-api-key" not in request.headers
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "gemini-1.5-pro"},
                        {"id": "models/gemini-1.5-flash"},
                        {"id": "gemini-unknown"},
                    ],
                },
            )

        # 2. Native Gemini listing calls
        if request.url.path == "/v1beta/models":
            # Key MUST be only in header x-goog-api-key, never in URL or authorization
            assert request.headers.get("x-goog-api-key") == GEMINI_KEY
            assert "authorization" not in request.headers
            assert GEMINI_KEY not in url_str

            page_token = request.url.params.get("pageToken")
            if page_token is None:
                # Page 1
                return httpx2.Response(
                    200,
                    json={
                        "models": [
                            {"name": "models/gemini-1.5-pro", "inputTokenLimit": 2097152},
                        ],
                        "nextPageToken": "page-token-2",
                    },
                )
            elif page_token == "page-token-2":
                # Page 2
                return httpx2.Response(
                    200,
                    json={
                        "models": [
                            {"name": "gemini-1.5-flash", "inputTokenLimit": 1048576},
                            {"name": "models/unlisted-native", "inputTokenLimit": 524288},
                        ],
                    },
                )
            return httpx2.Response(400, json={"error": "invalid page token"})

        return httpx2.Response(404)

    svc = make_service(handler)
    await svc.create_provider(
        ProviderCreate(
            name="gemini",
            kind="hosted",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
    )
    await svc.add_key("gemini", GEMINI_KEY)

    remote = await svc.list_remote_models("gemini")
    assert [(m.id, m.context_window) for m in remote] == [
        ("gemini-1.5-flash", 1048576),
        ("gemini-1.5-pro", 2097152),
        ("gemini-unknown", None),
    ]

    # Check requests made: key never in URL
    assert len(seen_requests) >= 3
    for req in seen_requests:
        assert GEMINI_KEY not in str(req.url)


async def test_gemini_native_call_fails_500_leaves_windows_empty(
    make_service: MakeService, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1beta/openai/models":
            return httpx2.Response(
                200,
                json={"object": "list", "data": [{"id": "gemini-1.5-pro"}]},
            )
        if request.url.path == "/v1beta/models":
            return httpx2.Response(500, json={"error": "internal error in native listing"})
        return httpx2.Response(404)

    svc = make_service(handler)
    await svc.create_provider(
        ProviderCreate(
            name="gemini",
            kind="hosted",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
    )
    await svc.add_key("gemini", GEMINI_KEY)

    with caplog.at_level(logging.WARNING):
        remote = await svc.list_remote_models("gemini")

    # Listing still succeeds, context window is None
    assert [(m.id, m.context_window) for m in remote] == [("gemini-1.5-pro", None)]
    # Logged without the key
    assert GEMINI_KEY not in caplog.text


async def test_gemini_native_call_network_error_scrubs_key(
    make_service: MakeService, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1beta/openai/models":
            return httpx2.Response(
                200,
                json={"object": "list", "data": [{"id": "gemini-1.5-pro"}]},
            )
        if request.url.path == "/v1beta/models":
            raise httpx2.ConnectError(f"connection error with key {GEMINI_KEY}")
        return httpx2.Response(404)

    svc = make_service(handler)
    await svc.create_provider(
        ProviderCreate(
            name="gemini",
            kind="hosted",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
    )
    await svc.add_key("gemini", GEMINI_KEY)

    with caplog.at_level(logging.WARNING):
        remote = await svc.list_remote_models("gemini")

    assert [(m.id, m.context_window) for m in remote] == [("gemini-1.5-pro", None)]
    assert GEMINI_KEY not in caplog.text


async def test_vllm_max_model_len_listing(make_service: MakeService) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/models":
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "meta-llama/Meta-Llama-3-8B-Instruct",
                            "object": "model",
                            "max_model_len": 8192,
                        },
                        {
                            "id": "mistralai/Mistral-7B-Instruct-v0.2",
                            "object": "model",
                            "max_model_len": 32768,
                        },
                    ],
                },
            )
        return httpx2.Response(404)

    svc = make_service(handler)
    await svc.create_provider(
        ProviderCreate(
            name="vllm",
            kind="self_hosted",
            base_url="http://localhost:8000/v1",
        )
    )

    remote = await svc.list_remote_models("vllm")
    assert [(m.id, m.context_window) for m in remote] == [
        ("meta-llama/Meta-Llama-3-8B-Instruct", 8192),
        ("mistralai/Mistral-7B-Instruct-v0.2", 32768),
    ]


async def test_add_model_stores_gemini_native_context_window(make_service: MakeService) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1beta/openai/models":
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "gemini-1.5-pro"},
                        {"id": "models/gemini-1.5-flash"},
                    ],
                },
            )
        if request.url.path == "/v1beta/models":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {"name": "models/gemini-1.5-pro", "inputTokenLimit": 2097152},
                        {"name": "gemini-1.5-flash", "inputTokenLimit": 1048576},
                    ],
                },
            )
        return httpx2.Response(404)

    svc = make_service(handler)
    await svc.create_provider(
        ProviderCreate(
            name="gemini",
            kind="hosted",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
    )
    await svc.add_key("gemini", GEMINI_KEY)

    # 1. Wire model ID matches directly, native has models/ prefix
    entry1 = await svc.add_model(ModelCreate(provider="gemini", wire_model_id="gemini-1.5-pro"))
    assert entry1.address == "gemini:gemini-1.5-pro"
    assert entry1.context_window == 2097152

    # 2. Wire model ID without models/ prefix, remote listing has models/ prefix
    entry2 = await svc.add_model(ModelCreate(provider="gemini", wire_model_id="gemini-1.5-flash"))
    assert entry2.address == "gemini:gemini-1.5-flash"
    assert entry2.context_window == 1048576

    # 3. Explicit context window is preserved
    entry3 = await svc.add_model(
        ModelCreate(provider="gemini", wire_model_id="custom-model", context_window=4096)
    )
    assert entry3.context_window == 4096
