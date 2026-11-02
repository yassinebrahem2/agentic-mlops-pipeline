from __future__ import annotations

import os
from typing import Any, cast

import httpx2
import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.providers.bridge import build_chat_model
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, Provider, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import resolve
from tests.conftest import MemoryKeyStore
from tests.providers.fake_clock import FakeClock

pytestmark = pytest.mark.live


async def _noop(_: str) -> None:
    pass


async def test_live_gemini_stream_usage(
    databases: Databases,
    keystore: MemoryKeyStore,
) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("set GEMINI_API_KEY to run this live test")

    async with httpx2.AsyncClient() as http:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        wire_model = "gemini-2.5-flash"
        client = build_chat_model(
            base_url=base_url,
            api_key=api_key,
            wire_model_id=wire_model,
            http=http,
            is_gemini=True,
        )

        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "lookup_status",
                    "description": "Look up service status",
                    "parameters": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                },
            }
        ]
        runnable = client.bind_tools(tools)  # pyright: ignore[reportUnknownMemberType]
        messages: list[BaseMessage] = [
            HumanMessage(content="Call lookup_status for service 'auth' now.")
        ]

        print(f"\nModel: {wire_model}")
        print("Chunk | Input Tokens | Output Tokens | Total Tokens")
        print("------|--------------|---------------|-------------")
        chunk_idx = 0
        chunk_usages: list[dict[str, Any]] = []
        async for chunk in runnable.astream(messages):
            aim = cast(AIMessageChunk, chunk)
            if hasattr(aim, "usage_metadata") and aim.usage_metadata:
                usage = cast(dict[str, Any], aim.usage_metadata)
                chunk_usages.append(usage)
                pt = usage.get("input_tokens")
                ct = usage.get("output_tokens")
                tt = usage.get("total_tokens")
                print(f"{chunk_idx:5d} | {pt!s:12s} | {ct!s:13s} | {tt!s:11s}")
            chunk_idx += 1

        assert len(chunk_usages) > 0

        clock = FakeClock()

        async def clear_attention(provider: Provider) -> None:
            await providers.clear_attention(provider.id)

        failover = KeyFailover(clock, on_success=clear_attention)
        providers = ProvidersService(databases, keystore, http, failover)
        await providers.create_provider(
            ProviderCreate(
                name="gemini",
                kind="hosted",
                base_url=base_url,
            )
        )
        await providers.add_key("gemini", api_key)
        await providers.add_model(ModelCreate(provider="gemini", wire_model_id=wire_model))
        model = await resolve(providers, f"gemini:{wire_model}")
        caller = ModelCaller(providers, http, failover)
        reply = await caller.call(model, messages, tools, _noop)

        assert reply.prompt_tokens is not None and reply.prompt_tokens > 0
        assert reply.completion_tokens is not None and reply.completion_tokens > 0
        last_reported_input = chunk_usages[-1]["input_tokens"]
        last_reported_output = chunk_usages[-1]["output_tokens"]
        assert reply.prompt_tokens == last_reported_input
        assert reply.completion_tokens == last_reported_output
