from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress

import httpx2
from sqlalchemy import delete, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore
from mlops_agents.credentials.scrub import error_text, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.providers import listing
from mlops_agents.providers.env_keys import detect_env_keys, known_env_key
from mlops_agents.providers.errors import ProviderCallError
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.keys import StoredKeys
from mlops_agents.providers.models import (
    DetectedKey,
    KeyHint,
    ModelCreate,
    ModelEntry,
    Provider,
    ProviderCreate,
    ProviderStatus,
    RemoteModel,
)
from mlops_agents.providers.rows import model_from_row, provider_from_row
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.providers.verification import probe_embedder, probe_model
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import models as models_table
from mlops_agents.storage.tables import provider_keys, providers


def _price_values(price: ModelPrice | None) -> dict[str, object]:
    return {
        "price_prompt_per_mtok": price.prompt_per_mtok if price else None,
        "price_completion_per_mtok": price.completion_per_mtok if price else None,
        "price_source": price.source if price else None,
    }


class ProvidersService:
    """`http` is the app's one client for provider calls, model calls included. Its owner (the
    app factory) closes it. langchain-openai's default client must never be used: it is cached
    for the whole process and bound to the first event loop that uses it. `failover` is the
    app's one key failover: a change to a provider's keys clears its key states, because key
    indices shift."""

    def __init__(
        self,
        databases: Databases,
        keystore: KeyStore,
        http: httpx2.AsyncClient,
        failover: KeyFailover,
    ) -> None:
        self._dbs = databases
        self._stored_keys = StoredKeys(databases, keystore)
        self._http = http
        self._failover = failover

    # Providers

    async def list_providers(self) -> list[Provider]:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(select(providers).order_by(providers.c.name))).mappings().all()
            )
            key_rows = (
                (await conn.execute(select(provider_keys).order_by(provider_keys.c.position)))
                .mappings()
                .all()
            )
        hints: dict[str, list[KeyHint]] = {}
        for k in key_rows:
            hints.setdefault(k["provider_id"], []).append(
                KeyHint(position=k["position"], hint=k["hint"])
            )
        return [provider_from_row(row, hints.get(row["id"], [])) for row in rows]

    async def get_provider(self, name: str) -> Provider:
        for provider in await self.list_providers():
            if provider.name == name:
                return provider
        raise DomainError("provider_not_found", f"There is no provider named {name}.", status=404)

    async def get_provider_by_id(self, provider_id: str) -> Provider:
        for provider in await self.list_providers():
            if provider.id == provider_id:
                return provider
        raise DomainError("provider_not_found", "That provider no longer exists.", status=404)

    async def create_provider(self, data: ProviderCreate) -> Provider:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            taken = await conn.scalar(select(providers.c.id).where(providers.c.name == data.name))
            if taken is not None:
                raise DomainError(
                    "provider_exists", f"A provider named {data.name} already exists.", status=409
                )
            await conn.execute(
                insert(providers).values(
                    id=new_id("prv"),
                    name=data.name,
                    kind=data.kind,
                    base_url=data.base_url,
                    status="ok",
                    status_detail=None,
                    created_at=now_iso(),
                )
            )
        return await self.get_provider(data.name)

    async def delete_provider(self, name: str) -> None:
        provider = await self.get_provider(name)
        await self._stored_keys.delete_provider(provider.id)
        self._failover.clear(provider)

    async def set_cooldown(self, name: str, minutes: int) -> Provider:
        """How long an exhausted key rests before it is tried again."""
        provider = await self.get_provider(name)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                update(providers)
                .where(providers.c.id == provider.id)
                .values(cooldown_minutes=minutes)
            )
        return await self.get_provider(name)

    async def mark_needs_attention(self, provider_id: str, detail: str) -> None:
        """Shown on the AI providers page; it never refuses a turn (Decision #384)."""
        await self._set_status(provider_id, "needs_attention", detail)

    async def clear_attention(self, provider_id: str) -> None:
        """The provider works again: a call succeeded, or Try again. Only a marked provider is
        written, so a healthy one costs no write."""
        await self._set_status(provider_id, "ok", None, only_if="needs_attention")

    async def _set_status(
        self,
        provider_id: str,
        status: ProviderStatus,
        detail: str | None,
        *,
        only_if: ProviderStatus | None = None,
    ) -> None:
        condition = providers.c.id == provider_id
        if only_if is not None:
            condition = condition & (providers.c.status == only_if)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                update(providers).where(condition).values(status=status, status_detail=detail)
            )

    # Keys

    async def keys_for(self, provider_id: str) -> list[str]:
        return await self._stored_keys.values_for(provider_id)

    async def secret_values(self) -> list[str]:
        return await self._stored_keys.all_values()

    async def add_key(self, name: str, value: str) -> Provider:
        value = value.strip()
        if not value:
            raise DomainError("key_empty", "Paste a key first.", status=422)
        provider = await self.get_provider(name)
        if value in await self.keys_for(provider.id):
            raise DomainError(
                "key_exists", "This key is already saved for this provider.", status=409
            )
        await self._get(provider, "/models", value)  # proves the key works before storing it
        await self._stored_keys.add(provider.id, value)
        self._failover.clear(provider)
        return await self.get_provider(name)

    async def remove_key(self, name: str, position: int) -> Provider:
        provider = await self.get_provider(name)
        await self._stored_keys.remove(provider.id, position)
        self._failover.clear(provider)
        return await self.get_provider(name)

    # Remote calls

    async def _get(self, provider: Provider, path: str, key: str | None) -> object:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        secrets_seen = [key] if key else []
        try:
            response = await self._http.get(provider.base_url + path, headers=headers, timeout=20)
        except httpx2.HTTPError as exc:
            reason = scrub_values(error_text(exc), secrets_seen)
            raise DomainError(
                "provider_unreachable", f"Could not reach {provider.name}: {reason}", status=502
            ) from None
        if response.status_code in (401, 403):
            raise DomainError("key_rejected", f"{provider.name} rejected this key.", status=422)
        if response.status_code >= 400:
            raise DomainError(
                "provider_error",
                f"{provider.name} answered with HTTP {response.status_code}.",
                status=502,
            )
        try:
            return response.json()
        except ValueError:
            raise DomainError(
                "provider_error", f"{provider.name} did not answer with JSON.", status=502
            ) from None

    async def list_remote_models(self, name: str) -> list[RemoteModel]:
        provider = await self.get_provider(name)
        keys = await self.keys_for(provider.id)
        if provider.kind == "hosted" and not keys:
            raise DomainError("no_keys", f"Add a key to {name} first.", status=409)
        remote = await listing.list_remote_models(
            provider,
            keys[0] if keys else None,
            get=self._get,
            http=self._http,
        )
        await self._store_provider_prices(provider, remote)
        return remote

    async def _store_provider_prices(self, provider: Provider, remote: list[RemoteModel]) -> None:
        """A listing's price goes to the provider's added models, unless a user price is set. A
        listing that gives no price changes nothing."""
        priced = [m for m in remote if m.price is not None]
        if not priced:
            return
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            for added in (
                await conn.execute(
                    select(models_table.c.address, models_table.c.wire_model_id).where(
                        models_table.c.provider_id == provider.id,
                        models_table.c.price_source.is_distinct_from("user"),
                    )
                )
            ).all():
                price = next(
                    (m.price for m in priced if listing.match_model_id(m.id, added.wire_model_id)),
                    None,
                )
                if price is not None:
                    await conn.execute(
                        update(models_table)
                        .where(models_table.c.address == added.address)
                        .values(**_price_values(price))
                    )

    # Models

    async def list_models(self) -> list[ModelEntry]:
        engine = await self._dbs.app()
        query = (
            select(models_table, providers.c.name.label("provider_name"))
            .join(providers, providers.c.id == models_table.c.provider_id)
            .order_by(models_table.c.address)
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [model_from_row(row) for row in rows]

    async def get_model(self, address: str) -> ModelEntry | None:
        return next((m for m in await self.list_models() if m.address == address), None)

    async def require_model(self, address: str) -> ModelEntry:
        entry = await self.get_model(address)
        if entry is None:
            raise DomainError("model_not_found", f"There is no model {address}.", status=404)
        return entry

    async def add_model(self, data: ModelCreate) -> ModelEntry:
        provider = await self.get_provider(data.provider)
        wire = data.wire_model_id.strip()
        address = f"{provider.name}:{wire}"
        if await self.get_model(address) is not None:
            raise DomainError("model_exists", f"{address} is already added.", status=409)
        try:
            remote = await self.list_remote_models(provider.name)
        except DomainError:
            remote = []  # the listing is only a convenience; the user can enter it later
        listed = next((m for m in remote if listing.match_model_id(m.id, wire)), None)
        context = data.context_window
        if data.model_type == "embedder":
            context = None
        elif context is None and listed is not None:
            context = listed.context_window
        price = listed.price if listed is not None else None
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(models_table).values(
                    address=address,
                    provider_id=provider.id,
                    wire_model_id=wire,
                    context_window=context,
                    tool_calling=0,
                    streaming=0,
                    availability="untested",
                    availability_detail=None,
                    verified_at=None,
                    model_type=data.model_type,
                    dimensions=None,
                    request_token_limit=None,
                    **_price_values(price),
                )
            )
        return await self.require_model(address)

    async def set_context_window(self, address: str, value: int) -> ModelEntry:
        if value <= 0:
            raise DomainError(
                "invalid_context_window", "Enter a positive number of tokens.", status=422
            )
        await self.require_model(address)
        await self._update_model(address, context_window=value)
        return await self.require_model(address)

    async def set_request_limit(self, address: str, value: int | None) -> ModelEntry:
        if value is not None and value <= 0:
            raise DomainError(
                "invalid_request_limit", "Enter a positive number of tokens.", status=422
            )
        await self.require_model(address)
        await self._update_model(address, request_token_limit=value)
        return await self.require_model(address)

    async def set_price(self, address: str, price: ModelPrice | None) -> ModelEntry:
        """Sets the user's price for a model, or clears the price (a later listing may then
        bring the provider's back)."""
        await self.require_model(address)
        await self._update_model(address, **_price_values(price))
        return await self.require_model(address)

    async def delete_model(self, address: str) -> None:
        # agent_model_defaults rows that name this model stay; model assignment (agent-runtime
        # plan) reports them with a "choose another model" message.
        await self.require_model(address)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(models_table).where(models_table.c.address == address))

    async def record_verification(
        self,
        address: str,
        *,
        tool_calling: bool,
        streaming: bool,
        detail: str | None,
        dimensions: int | None = None,
    ) -> ModelEntry:
        await self._update_model(
            address,
            tool_calling=int(tool_calling),
            streaming=int(streaming),
            availability="verified",
            availability_detail=detail,
            verified_at=now_iso(),
            dimensions=dimensions,
        )
        return await self.require_model(address)

    async def mark_model_problem(self, address: str, detail: str) -> None:
        await self._update_model(
            address, availability="needs_attention", availability_detail=detail
        )

    async def _update_model(self, address: str, **values: object) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                update(models_table).where(models_table.c.address == address).values(**values)
            )

    async def verify_model(self, address: str) -> ModelEntry:
        entry = await self.require_model(address)
        provider = await self.get_provider_by_id(entry.provider_id)
        keys = await self.keys_for(provider.id)
        if provider.kind == "hosted" and not keys:
            raise DomainError("no_keys", f"Add a key to {provider.name} first.", status=409)
        first_key = keys[0] if keys else None
        # Stores a published price; a failing listing must not fail the check (the probe below
        # reports its own errors).
        with suppress(DomainError):
            await self.list_remote_models(provider.name)

        if entry.model_type == "embedder":
            try:
                probe = await probe_embedder(
                    provider.base_url, first_key, entry.wire_model_id, http=self._http
                )
            except ProviderCallError as err:
                await self.mark_model_problem(address, err.message)
                raise DomainError(
                    "verification_failed", f"Checking {address} failed: {err.message}", status=502
                ) from None
            return await self.record_verification(
                address,
                tool_calling=False,
                streaming=False,
                detail=None,
                dimensions=probe.dimensions,
            )

        try:
            result = await probe_model(
                provider.base_url, first_key, entry.wire_model_id, http=self._http
            )
        except ProviderCallError as err:
            await self.mark_model_problem(address, err.message)
            raise DomainError(
                "verification_failed", f"Checking {address} failed: {err.message}", status=502
            ) from None
        return await self.record_verification(
            address,
            tool_calling=result.tool_calling,
            streaming=result.streaming,
            detail=result.detail,
            dimensions=None,
        )

    # Environment keys

    async def detect_env_keys(self, env: Mapping[str, str] | None = None) -> list[DetectedKey]:
        source = os.environ if env is None else env
        existing = {p.name for p in await self.list_providers()}
        return detect_env_keys(source, existing)

    async def import_env_key(self, env_var: str, env: Mapping[str, str] | None = None) -> Provider:
        source = os.environ if env is None else env
        known = known_env_key(env_var)
        if known is None:
            raise DomainError("unknown_env_key", f"{env_var} is not a recognised key.", status=422)
        value = source.get(env_var)
        if not value:
            raise DomainError("env_key_missing", f"{env_var} is not set.", status=404)
        provider_name, base_url = known
        if provider_name not in {p.name for p in await self.list_providers()}:
            await self.create_provider(
                ProviderCreate(name=provider_name, kind="hosted", base_url=base_url)
            )
        return await self.add_key(provider_name, value)
