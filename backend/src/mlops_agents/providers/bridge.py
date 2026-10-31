from __future__ import annotations

from dataclasses import dataclass, field

import httpx2
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

# Self-hosted servers usually ignore the key, but the OpenAI client refuses to start without one.
KEYLESS = "not-needed"
EMBED_BATCH = 64


def build_chat_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """The only way the app creates an LLM client. Retries are handled by the caller. `http` is
    the app's shared client (see ProvidersService); without it langchain-openai would use a
    process-wide cached client tied to the first event loop."""
    return ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(api_key or KEYLESS),
        model=wire_model_id,
        streaming=True,
        stream_usage=True,
        max_retries=0,
        timeout=timeout,
        http_async_client=http,
        max_completion_tokens=max_tokens,
    )


def build_embeddings(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        base_url=base_url,
        api_key=SecretStr(api_key or KEYLESS),
        model=wire_model_id,
        max_retries=0,
        timeout=timeout,
        http_async_client=http,
        check_embedding_ctx_length=False,
        chunk_size=EMBED_BATCH,
    )


@dataclass(frozen=True)
class ResolvedModel:
    address: str
    provider_id: str
    provider_name: str
    base_url: str
    wire_model_id: str
    context_window: int
    keys: tuple[str, ...] = field(repr=False)
    request_token_limit: int | None = None

    def build(self, http: httpx2.AsyncClient, key_index: int = 0) -> ChatOpenAI:
        from mlops_agents.agents.runtime.budget import reply_reserve_tokens, request_budget_tokens

        key = self.keys[key_index] if self.keys else None
        max_tokens = (
            reply_reserve_tokens(request_budget_tokens(self))
            if self.request_token_limit is not None
            else None
        )
        return build_chat_model(
            self.base_url,
            key,
            self.wire_model_id,
            http=http,
            max_tokens=max_tokens,
        )
