from __future__ import annotations

from dataclasses import dataclass, field

import httpx2
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

# Self-hosted servers usually ignore the key, but the OpenAI client refuses to start without one.
KEYLESS = "not-needed"


def build_chat_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 120.0,
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

    def build(self, http: httpx2.AsyncClient, key_index: int = 0) -> ChatOpenAI:
        key = self.keys[key_index] if self.keys else None
        return build_chat_model(self.base_url, key, self.wire_model_id, http=http)
