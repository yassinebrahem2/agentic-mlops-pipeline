from typing import cast

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.providers.bridge import EMBED_BATCH, build_embeddings
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.providers.verification import EmbedderProbe, probe_embedder
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def _service(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    http: httpx2.AsyncClient,
) -> ProvidersService:
    svc = ProvidersService(databases, keystore, http, KeyFailover())
    await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    return svc


async def test_probe_embedder_succeeds(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    probe = await probe_embedder(fake_openai.base_url, None, "fake-embed", http=model_http)
    assert isinstance(probe, EmbedderProbe)
    assert probe.dimensions == 16


async def test_embedder_verifies_and_records_traits(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    entry = await svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-embed", model_type="embedder")
    )
    assert entry.model_type == "embedder"
    assert entry.context_window is None
    assert entry.dimensions is None
    assert entry.availability == "untested"
    assert entry.problem is not None

    verified = await svc.verify_model("fake:fake-embed")
    assert verified.availability == "verified"
    assert not verified.tool_calling
    assert not verified.streaming
    assert verified.dimensions == 16
    assert verified.context_window is None
    assert verified.problem is None


async def test_embedder_not_found_marks_needs_attention(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    # fake-no-tools is a chat model, so /embeddings returns 404
    await svc.add_model(
        ModelCreate(provider="fake", wire_model_id="fake-no-tools", model_type="embedder")
    )
    with pytest.raises(DomainError) as err:
        await svc.verify_model("fake:fake-no-tools")
    assert err.value.code == "verification_failed"
    assert "Unknown model fake-no-tools" in err.value.message

    entry = await svc.require_model("fake:fake-no-tools")
    assert entry.availability == "needs_attention"
    assert entry.problem is not None
    assert "fake:fake-no-tools failed" in entry.problem


async def test_embedder_auth_fail_scrubs_key(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http, KeyFailover())
    await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    secret_key = "super_secret_embedder_key"
    await svc.add_key("fake-hosted", secret_key)
    fake_openai.state.required_key = "different_key"

    await svc.add_model(
        ModelCreate(provider="fake-hosted", wire_model_id="fake-embed", model_type="embedder")
    )
    with pytest.raises(DomainError) as err:
        await svc.verify_model("fake-hosted:fake-embed")
    assert err.value.code == "verification_failed"
    assert secret_key not in err.value.message

    entry = await svc.require_model("fake-hosted:fake-embed")
    assert entry.availability == "needs_attention"
    assert secret_key not in str(entry.availability_detail)


async def test_chat_model_verify_leaves_dimensions_null(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    await svc.add_model(ModelCreate(provider="fake", wire_model_id="fake-tools", model_type="chat"))
    verified = await svc.verify_model("fake:fake-tools")
    assert verified.availability == "verified"
    assert verified.dimensions is None


async def test_request_carries_raw_strings_and_splits_large_batch(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    embeddings = build_embeddings(fake_openai.base_url, None, "fake-embed", http=model_http)
    # 1. Single query -> input is raw str
    fake_openai.state.requests.clear()
    await embeddings.aembed_query("hello world")
    assert len(fake_openai.state.requests) == 1
    req_input = fake_openai.state.requests[0]["input"]
    assert isinstance(req_input, (str, list))
    if isinstance(req_input, list):
        raw_list = cast(list[object], req_input)
        assert all(isinstance(x, str) for x in raw_list)
    else:
        assert isinstance(req_input, str)

    # 2. Batch larger than EMBED_BATCH splits into multiple requests
    fake_openai.state.requests.clear()
    texts = [f"item {i}" for i in range(EMBED_BATCH + 5)]
    await embeddings.aembed_documents(texts)
    assert len(fake_openai.state.requests) == 2
    assert len(fake_openai.state.requests[0]["input"]) == EMBED_BATCH
    assert len(fake_openai.state.requests[1]["input"]) == 5
