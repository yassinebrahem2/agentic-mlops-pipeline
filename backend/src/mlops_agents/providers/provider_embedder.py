"""ProviderEmbedder: calls a provider's embedding model with key failover (stage 3 spec)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx2

from mlops_agents.providers.bridge import build_embeddings
from mlops_agents.providers.embedder import EmbedderUnavailable
from mlops_agents.providers.errors import classify
from mlops_agents.providers.failover import (
    AllKeysExhausted,
    KeyFailover,
    RateLimited,
    no_usable_key,
)
from mlops_agents.providers.models import Provider
from mlops_agents.providers.service import ProvidersService


class ProviderEmbedder:
    """Implements Embedder protocol using an AI provider and KeyFailover."""

    def __init__(
        self,
        address: str,
        dimensions: int,
        provider: Provider,
        wire_model_id: str,
        failover: KeyFailover,
        providers: ProvidersService,
        http: httpx2.AsyncClient,
    ) -> None:
        self.address = address
        self.dimensions = dimensions
        self._provider = provider
        self._wire_model_id = wire_model_id
        self._failover = failover
        self._providers = providers
        self._http = http

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        async def attempt(key_index: int | None) -> list[list[float]]:
            key: str | None = None
            if key_index is not None:
                keys = await self._providers.keys_for(self._provider.id)
                key = keys[key_index]
            client = build_embeddings(
                self._provider.base_url,
                key,
                self._wire_model_id,
                http=self._http,
            )
            return await client.aembed_documents(list(texts))

        try:
            result = await self._failover.call(
                self._provider,
                self._wire_model_id,
                attempt,
                cooldown_minutes=self._provider.cooldown_minutes,
                wait=False,
            )
        except AllKeysExhausted as exc:
            desc = self._failover.describe_keys(exc.keys)
            headline = no_usable_key(self._provider.name, self._wire_model_id)
            raise EmbedderUnavailable(f"{headline}. {desc}") from exc
        except RateLimited as exc:
            raise EmbedderUnavailable(
                f"Every key of {self._provider.name} is rate-limited right now; search uses full text."
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            keys = await self._providers.keys_for(self._provider.id)
            classified = classify(exc, keys)
            raise EmbedderUnavailable(classified.message) from None

        if len(result) != len(texts):
            raise EmbedderUnavailable(
                f"The embedder {self.address} returned {len(result)} embeddings for {len(texts)} texts. "
                "Verify it again in AI providers."
            )
        for vec in result:
            if len(vec) != self.dimensions:
                raise EmbedderUnavailable(
                    f"The embedder {self.address} now answers {len(vec)} dimensions. "
                    "Verify it again in AI providers."
                )
        return result
