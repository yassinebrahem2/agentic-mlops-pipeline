from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Mapping

import httpx2
from pydantic import BaseModel, ValidationError
from sqlalchemy import RowMapping, delete, func, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore
from mlops_agents.credentials.scrub import error_text, mask_key, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.providers.errors import ProviderCallError
from mlops_agents.providers.models import (
    DetectedKey,
    KeyHint,
    ModelCreate,
    ModelEntry,
    Provider,
    ProviderCreate,
    RemoteModel,
)
from mlops_agents.providers.verification import probe_model
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import models as models_table
from mlops_agents.storage.tables import provider_keys, providers

# Environment variables recognised by "detect keys": name -> (provider name, base URL).
KNOWN_ENV_KEYS: dict[str, tuple[str, str]] = {
    "GROQ_API_KEY": ("groq", "https://api.groq.com/openai/v1"),
    "GEMINI_API_KEY": ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "OPENROUTER_API_KEY": ("openrouter", "https://openrouter.ai/api/v1"),
}
_ENV_VAR = re.compile(r"^(?P<base>[A-Z0-9_]+?)(?:_\d+)?$")


class _ListedModel(BaseModel):
    """One entry of a provider's /models answer. Providers name the context window differently."""

    id: str
    context_window: object = None
    context_length: object = None
    max_context_length: object = None

    def context(self) -> int | None:
        for value in (self.context_window, self.context_length, self.max_context_length):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return None


class _ModelList(BaseModel):
    data: list[object]


class ProvidersService:
    """`http` is the app's one client for provider calls, model calls included. Its owner (the
    app factory) closes it. langchain-openai's default client must never be used: it is cached
    for the whole process and bound to the first event loop that uses it."""

    def __init__(self, databases: Databases, keystore: KeyStore, http: httpx2.AsyncClient) -> None:
        self._dbs = databases
        self._keys = keystore
        self._http = http

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
        return [_provider(row, hints.get(row["id"], [])) for row in rows]

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
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            refs = list(
                await conn.scalars(
                    select(provider_keys.c.keystore_ref).where(
                        provider_keys.c.provider_id == provider.id
                    )
                )
            )
            await conn.execute(delete(providers).where(providers.c.id == provider.id))
        for ref in refs:
            await asyncio.to_thread(self._keys.delete, ref)

    async def mark_needs_attention(self, provider_id: str, detail: str) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                update(providers)
                .where(providers.c.id == provider_id)
                .values(status="needs_attention", status_detail=detail)
            )

    # Keys

    async def keys_for(self, provider_id: str) -> list[str]:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            refs = list(
                await conn.scalars(
                    select(provider_keys.c.keystore_ref)
                    .where(provider_keys.c.provider_id == provider_id)
                    .order_by(provider_keys.c.position)
                )
            )
        values = [await asyncio.to_thread(self._keys.get, ref) for ref in refs]
        return [v for v in values if v]

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
        ref = f"provider/{provider.id}/{secrets.token_hex(8)}"
        await asyncio.to_thread(self._keys.set, ref, value)
        engine = await self._dbs.app()
        try:
            async with engine.begin() as conn:
                last = await conn.scalar(
                    select(func.max(provider_keys.c.position)).where(
                        provider_keys.c.provider_id == provider.id
                    )
                )
                await conn.execute(
                    insert(provider_keys).values(
                        provider_id=provider.id,
                        position=(last or 0) + 1,
                        keystore_ref=ref,
                        hint=mask_key(value),
                        created_at=now_iso(),
                    )
                )
                await conn.execute(
                    update(providers)
                    .where(providers.c.id == provider.id)
                    .values(status="ok", status_detail=None)
                )
        except BaseException as db_err:
            try:
                await asyncio.to_thread(self._keys.delete, ref)
            except Exception as del_err:
                # The database error is still the one to report; the failed clean-up rides along.
                db_err.add_note(
                    f"The new key could not be removed from the key store either: {del_err}"
                )
            raise
        return await self.get_provider(name)

    async def remove_key(self, name: str, position: int) -> Provider:
        provider = await self.get_provider(name)
        engine = await self._dbs.app()
        where = (provider_keys.c.provider_id == provider.id) & (
            provider_keys.c.position == position
        )
        async with engine.begin() as conn:
            ref = await conn.scalar(select(provider_keys.c.keystore_ref).where(where))
            if ref is None:
                raise DomainError("key_not_found", "That key is not saved.", status=404)
            await conn.execute(delete(provider_keys).where(where))
        await asyncio.to_thread(self._keys.delete, ref)
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
        payload = await self._get(provider, "/models", keys[0] if keys else None)
        try:
            listing = _ModelList.model_validate(payload)
        except ValidationError:
            raise DomainError(
                "provider_error", f"{provider.name} did not answer with a model list.", status=502
            ) from None
        found: list[RemoteModel] = []
        for item in listing.data:
            try:
                listed = _ListedModel.model_validate(item)
            except ValidationError:
                continue  # an entry without a string id cannot be added; the others still can
            found.append(RemoteModel(id=listed.id, context_window=listed.context()))
        return sorted(found, key=lambda m: m.id)

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
        return [_model(row) for row in rows]

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
        context = data.context_window
        if context is None:
            try:
                remote = await self.list_remote_models(provider.name)
            except DomainError:
                remote = []  # the listing is only a convenience; the user can enter it later
            context = next((m.context_window for m in remote if m.id == wire), None)
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

    async def delete_model(self, address: str) -> None:
        # agent_model_defaults rows that name this model stay; model assignment (agent-runtime
        # plan) reports them with a "choose another model" message.
        await self.require_model(address)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(models_table).where(models_table.c.address == address))

    async def record_verification(
        self, address: str, *, tool_calling: bool, streaming: bool, detail: str | None
    ) -> ModelEntry:
        await self._update_model(
            address,
            tool_calling=int(tool_calling),
            streaming=int(streaming),
            availability="verified",
            availability_detail=detail,
            verified_at=now_iso(),
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
        try:
            result = await probe_model(
                provider.base_url, keys[0] if keys else None, entry.wire_model_id, http=self._http
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
        )

    # Environment keys

    async def detect_env_keys(self, env: Mapping[str, str] | None = None) -> list[DetectedKey]:
        source = os.environ if env is None else env
        existing = {p.name for p in await self.list_providers()}
        found: list[DetectedKey] = []
        for base, (provider_name, base_url) in KNOWN_ENV_KEYS.items():
            pattern = re.compile(rf"^{re.escape(base)}(?:_(?P<num>\d+))?$")
            candidates: list[tuple[tuple[int, int], str, str]] = []
            for var, value in source.items():
                if not value:
                    continue
                match = pattern.match(var)
                if match:
                    num_str = match.group("num")
                    sort_key = (-1, 0) if num_str is None else (int(num_str), 1)
                    candidates.append((sort_key, var, value))
            candidates.sort(key=lambda c: c[0])
            for _, var, value in candidates:
                found.append(
                    DetectedKey(
                        env_var=var,
                        provider_name=provider_name,
                        base_url=base_url,
                        hint=mask_key(value),
                        provider_exists=provider_name in existing,
                    )
                )
        return found

    async def import_env_key(self, env_var: str, env: Mapping[str, str] | None = None) -> Provider:
        source = os.environ if env is None else env
        match = _ENV_VAR.match(env_var)
        known = KNOWN_ENV_KEYS.get(match.group("base")) if match else None
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


def _provider(row: RowMapping, keys: list[KeyHint]) -> Provider:
    return Provider(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        base_url=row["base_url"],
        status=row["status"],
        status_detail=row["status_detail"],
        keys=keys,
    )


def _model(row: RowMapping) -> ModelEntry:
    return ModelEntry(
        address=row["address"],
        provider_id=row["provider_id"],
        provider_name=row["provider_name"],
        wire_model_id=row["wire_model_id"],
        context_window=row["context_window"],
        tool_calling=bool(row["tool_calling"]),
        streaming=bool(row["streaming"]),
        availability=row["availability"],
        availability_detail=row["availability_detail"],
        verified_at=row["verified_at"],
    )
