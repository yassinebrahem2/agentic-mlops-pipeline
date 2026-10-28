"""A provider and model on the fake server, with a model caller on a real `KeyFailover` driven
by a `FakeClock`, for the model caller tests; and clients that fail after streaming text."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, Provider, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import FakeClock


@dataclass
class CallerEnv:
    providers: ProvidersService
    model: ResolvedModel
    caller: ModelCaller
    failover: KeyFailover
    clock: FakeClock


async def resolve(providers: ProvidersService, address: str) -> ResolvedModel:
    """The model as an agent's turn resolves it: its provider and the keys stored now."""
    entry = await providers.require_model(address)
    provider = await providers.get_provider_by_id(entry.provider_id)
    assert entry.context_window is not None
    return ResolvedModel(
        address=entry.address,
        provider_id=provider.id,
        provider_name=provider.name,
        base_url=provider.base_url,
        wire_model_id=entry.wire_model_id,
        context_window=entry.context_window,
        keys=tuple(await providers.keys_for(provider.id)),
    )


async def build_caller_env(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    http: httpx2.AsyncClient,
    *,
    name: str = "fake",
    wire_model_ids: Sequence[str] = ("fake-tools",),
    keys: Sequence[str] = (),
) -> CallerEnv:
    """The first of `wire_model_ids` is `model`; every one is added and verified. The fake's
    request log is cleared after setup."""
    clock = FakeClock()

    async def clear_attention(provider: Provider) -> None:  # wired as `build_services` does
        await providers.clear_attention(provider.id)

    failover = KeyFailover(clock, on_success=clear_attention)
    providers = ProvidersService(databases, keystore, http, failover)
    kind = "hosted" if keys else "self_hosted"
    await providers.create_provider(ProviderCreate(name=name, kind=kind, base_url=fake.base_url))
    for key in keys:
        await providers.add_key(name, key)
    for wire_model_id in wire_model_ids:
        await providers.add_model(ModelCreate(provider=name, wire_model_id=wire_model_id))
        await providers.verify_model(f"{name}:{wire_model_id}")
    model = await resolve(providers, f"{name}:{wire_model_ids[0]}")
    fake.state.requests.clear()
    fake.state.request_headers.clear()
    return CallerEnv(
        providers=providers,
        model=model,
        caller=ModelCaller(providers, http, failover),
        failover=failover,
        clock=clock,
    )


def keys_used(fake: RunningFake) -> list[str]:
    """The bearer key of every chat request the fake received, in order."""
    return [h["authorization"].removeprefix("Bearer ") for h in fake.state.request_headers]


class ScriptedClient:
    """A chat client that streams `chunks`, then fails with `error` when one is given (a 429
    after text, say). The fake server cannot fail after text over HTTP: its status line goes
    out before any text."""

    def __init__(self, chunks: list[AIMessageChunk], error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    def bind_tools(self, _: list[dict[str, Any]]) -> ScriptedClient:
        return self

    async def astream(self, _: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def script_builds(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | ScriptedClient | None]
) -> list[int]:
    """The caller's next client builds, one outcome each, in order: an exception gives a client
    that streams some text then fails with it; a `ScriptedClient` is used as it is; None, or a
    build after the outcomes are used up, the real client on the fake server. Returns the key
    index of each build."""
    built: list[int] = []
    real_build = ResolvedModel.build
    pending = list(outcomes)

    def build(self: ResolvedModel, http: httpx2.AsyncClient, key_index: int = 0) -> Any:
        built.append(key_index)
        outcome = pending.pop(0) if pending else None
        if outcome is None:
            return real_build(self, http, key_index)
        if isinstance(outcome, ScriptedClient):
            return outcome
        return ScriptedClient([AIMessageChunk(content="Partial ")], outcome)

    monkeypatch.setattr(ResolvedModel, "build", build)
    return built
