import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

from mlops_agents.providers.embedder import EmbedderUnavailable
from mlops_agents.providers.failover import AllKeysExhausted, KeyFailover, KeyState, RateLimited
from mlops_agents.providers.models import Provider, ProviderCreate
from mlops_agents.providers.provider_embedder import ProviderEmbedder
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import FakeState, RunningFake, fake_embedding, start_fake_server


class RecordingFailover(KeyFailover):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def call[T](
        self,
        provider: Provider,
        model: str,
        attempt: Callable[[int | None], Awaitable[T]],
        *,
        cooldown_minutes: int,
        wait: bool = True,
    ) -> T:
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "cooldown_minutes": cooldown_minutes,
                "wait": wait,
            }
        )
        return await super().call(
            provider, model, attempt, cooldown_minutes=cooldown_minutes, wait=wait
        )


class ExhaustedFailover(KeyFailover):
    async def call[T](
        self,
        provider: Provider,
        model: str,
        attempt: Callable[[int | None], Awaitable[T]],
        *,
        cooldown_minutes: int,
        wait: bool = True,
    ) -> T:
        states = [KeyState(key_index=0, status="billing", since=None)]
        raise AllKeysExhausted(states)


class RateLimitedFailover(KeyFailover):
    async def call[T](
        self,
        provider: Provider,
        model: str,
        attempt: Callable[[int | None], Awaitable[T]],
        *,
        cooldown_minutes: int,
        wait: bool = True,
    ) -> T:
        raise RateLimited(retry_after_s=30.0)


async def test_empty_input_makes_no_request(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    failover = RecordingFailover()
    embedder = ProviderEmbedder(
        address="fake:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    fake_openai.state.requests.clear()
    res = await embedder.embed([])
    assert res == []
    assert len(failover.calls) == 0
    assert len(fake_openai.state.requests) == 0


async def test_embed_returns_vectors_equal_to_fake_embedding(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    texts = ["hello world", "model monitoring"]
    res = await embedder.embed(texts)
    assert len(res) == 2
    assert res[0] == fake_embedding("hello world", 16)
    assert res[1] == fake_embedding("model monitoring", 16)


async def test_two_keys_failover_on_rate_limit(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    await svc.add_key("fake-hosted", "key_1")
    await svc.add_key("fake-hosted", "key_2")
    provider = await svc.get_provider("fake-hosted")

    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake-hosted:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    # First key gets 429, second key succeeds
    fake_openai.state.rate_limit_remaining = 1
    res = await embedder.embed(["test failover"])
    assert len(res) == 1
    assert res[0] == fake_embedding("test failover", 16)
    assert len(fake_openai.state.request_headers) == 2
    assert fake_openai.state.request_headers[0]["authorization"] == "Bearer key_1"
    assert fake_openai.state.request_headers[1]["authorization"] == "Bearer key_2"


async def test_self_hosted_provider_called_with_attempt_none(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    failover = RecordingFailover()
    embedder = ProviderEmbedder(
        address="fake:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    await embedder.embed(["hello"])
    assert len(failover.calls) == 1
    assert failover.calls[0]["cooldown_minutes"] == provider.cooldown_minutes
    assert failover.calls[0]["wait"] is False
    assert fake_openai.state.request_headers[-1]["authorization"] == "Bearer not-needed"


async def test_failover_all_keys_exhausted_mapping(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    failover = ExhaustedFailover()
    embedder = ProviderEmbedder(
        address="fake-hosted:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["hello"])
    assert "fake-hosted: no usable key for fake-embed." in str(err.value)
    assert "Key 1: billing, out of credit." in str(err.value)


async def test_failover_rate_limited_mapping(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    failover = RateLimitedFailover()
    embedder = ProviderEmbedder(
        address="fake-hosted:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["hello"])
    assert "rate-limited" in str(err.value)
    assert "search uses full text" in str(err.value)


async def test_permanent_error_scrubs_secrets(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    secret_key = "secret_embed_val_123"
    await svc.add_key("fake-hosted", secret_key)
    provider = await svc.get_provider("fake-hosted")

    fake_openai.state.auth_fail = True
    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake-hosted:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["hello"])
    assert secret_key not in str(err.value)


async def test_wrong_dimensions_raises_verify_again(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake-hosted", kind="hosted", base_url=fake_openai.base_url)
    )
    await svc.add_key("fake-hosted", "key_1")
    await svc.add_key("fake-hosted", "key_2")
    provider = await svc.get_provider("fake-hosted")
    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake-hosted:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    fake_openai.state.embedding_wrong_dimensions = 8
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["hello"])
    assert "8 dimensions" in str(err.value)
    assert "Verify it again in AI providers." in str(err.value)
    assert len(fake_openai.state.requests) == 1
    assert all(st.status == "ok" for st in failover.states(provider, "fake-embed"))


async def test_returned_count_mismatch_raises_unavailable(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    fake_openai.state.embedding_truncate_count = 1
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["first text", "second text"])
    assert "returned 1 embeddings for 2 texts" in str(err.value)
    assert "Verify it again in AI providers." in str(err.value)


async def test_unreachable_provider_raises_scrubbed(
    databases: Databases,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
) -> None:
    running = start_fake_server(FakeState())
    svc = ProvidersService(databases, keystore, model_http)
    secret = "secret_key_unreachable"
    provider = await svc.create_provider(
        ProviderCreate(name="fake-dead", kind="hosted", base_url=running.base_url)
    )
    await svc.add_key("fake-dead", secret)
    provider = await svc.get_provider("fake-dead")

    # Stop server to make it unreachable
    running.stop()

    failover = KeyFailover()
    embedder = ProviderEmbedder(
        address="fake-dead:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=failover,
        providers=svc,
        http=model_http,
    )
    with pytest.raises(EmbedderUnavailable) as err:
        await embedder.embed(["hello"])
    assert secret not in str(err.value)


async def test_cancellation_propagates(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = ProvidersService(databases, keystore, model_http)
    provider = await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )

    class CancellingFailover(KeyFailover):
        async def call[T](
            self,
            provider: Provider,
            model: str,
            attempt: Callable[[int | None], Awaitable[T]],
            *,
            cooldown_minutes: int,
            wait: bool = True,
        ) -> T:
            raise asyncio.CancelledError()

    embedder = ProviderEmbedder(
        address="fake:fake-embed",
        dimensions=16,
        provider=provider,
        wire_model_id="fake-embed",
        failover=CancellingFailover(),
        providers=svc,
        http=model_http,
    )
    with pytest.raises(asyncio.CancelledError):
        await embedder.embed(["hello"])
