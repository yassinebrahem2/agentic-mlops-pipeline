# Stage 1 — Providers Implementation Plan (DRAFT)

> **DRAFT: not ready to implement.** This plan sits in `drafts/` until both items below are done. Then it moves to `docs/superpowers/plans/` on its own `plan/stage-1-<topic>` branch and gets its issues.
> 1. **Advisor review.** Round 1 found blockers, and they are fixed. Rounds must continue until one finds no blocking issues.
> 2. **Check against the merged backend-core code.** backend-core changed while it was implemented (see PR #10, "done differently"). Every name in this plan's Consumes lines and test fixtures must match the code on `main`.


> **For agentic workers:** Implement this plan task by task, in order. Each task lists its exact files, the interfaces it consumes and produces, its tests (the task's stop condition) and the commands that prove it works. Claude Code users may use superpowers:executing-plans or superpowers:subagent-driven-development; nothing here requires them. Steps use checkbox (`- [ ]`) syntax. Team rules (claiming tasks, commits, PRs) are in `AGENTS.md`.

**Goal:** The AI providers registry (hosted and self-hosted, keys in the key store, models with context windows), a fake OpenAI-compatible server for tests, model verification by a streamed tool call, provider error classification, and the single bridge to LangChain chat models.

**Branch:** `plan/stage-1-providers`. **Depends on:** backend-core (branch from main after it is merged).

**Read first:** `docs/superpowers/plans/2026-10-16-stage-1-overview.md` (global constraints, review focus, file structure), the spec `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`, and `docs/roadmap.md`. The global constraints apply to every task below.

## Tasks

| Task | Title | Depends on |
|---|---|---|
| 1 | Providers registry | none |
| 2 | Fake provider, verification, error classes, bridge | 1 |

---

### Task 1: Providers registry

**Files:**
- Create: `backend/src/mlops_agents/providers/__init__.py` (empty), `models.py`, `service.py`
- Modify: `backend/pyproject.toml`, `backend/uv.lock` (add `httpx2`)
- Test: `backend/tests/providers/__init__.py` (empty), `test_service.py`

**Interfaces:**
- Consumes: `Databases` (backend-core plan, Task 3), `KeyStore`, `scrub_values`, `error_text`, `mask_key`, `base_url_problem` (backend-core plan, Task 4), `DomainError`, `new_id`, `now_iso` (backend-core plan, Task 2); test fixtures `databases`, `keystore`, `paths` and the class `MemoryKeyStore` from `tests/conftest.py` (backend-core plan, Task 5).
- Produces:
  - `models.py`: `ProviderKind`, `ProviderCreate(name, kind, base_url)`, `KeyHint(position, hint)`, `Provider(id, name, kind, base_url, status, status_detail, keys: list[KeyHint])`, `ModelCreate(provider, wire_model_id, context_window: int | None)`, `ModelEntry(address, provider_id, provider_name, wire_model_id, context_window, tool_calling, streaming, availability, availability_detail, verified_at)`, `RemoteModel(id, context_window)`, `DetectedKey(env_var, provider_name, base_url, hint, provider_exists)`.
  - `ProvidersService(databases: Databases, keystore: KeyStore, http: httpx2.AsyncClient)` with async methods: `list_providers() -> list[Provider]`, `get_provider(name) -> Provider`, `get_provider_by_id(provider_id) -> Provider`, `create_provider(data: ProviderCreate) -> Provider`, `delete_provider(name) -> None`, `add_key(name, value) -> Provider`, `remove_key(name, position) -> Provider`, `keys_for(provider_id) -> list[str]`, `list_remote_models(name) -> list[RemoteModel]`, `add_model(data: ModelCreate) -> ModelEntry`, `list_models() -> list[ModelEntry]`, `get_model(address) -> ModelEntry | None`, `require_model(address) -> ModelEntry`, `set_context_window(address, value) -> ModelEntry`, `delete_model(address) -> None`, `record_verification(address, *, tool_calling, streaming, detail) -> ModelEntry`, `mark_model_problem(address, detail) -> None`, `mark_needs_attention(provider_id, detail) -> None`, `detect_env_keys(env=None) -> list[DetectedKey]`, `import_env_key(env_var, env=None) -> Provider`. Task 2 adds `verify_model(address) -> ModelEntry`.
  - The `http` client: one `httpx2.AsyncClient` per app, created and closed by the app factory (api plan), carries every provider and model call. `httpx2` is the HTTP library the `openai` SDK uses since 3.x; models must get the app's client because langchain-openai's default one is cached for the whole process and bound to the first event loop that used it (the tests, which run one event loop per test, fail with "Event loop is closed" without this).
  - `delete_model` and `delete_provider` leave `agent_model_defaults` rows that name a deleted model. Model assignment (agent-runtime plan) reports such a default with the spec's "choose another model" message, as it does for a project override.

- [ ] **Step 0: Add the dependency**

Run: `cd backend && uv add "httpx2>=2.13.1"`
Expected: `pyproject.toml` lists `httpx2>=2.13.1` and `uv.lock` is updated. It is already installed through `openai`; this declares it because the app imports it.


- [ ] **Step 1: Write the failing tests** `backend/tests/providers/test_service.py`:

```python
import json
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest
from pydantic import ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore

GOOD = "gsk_good_key_123456"
Handler = Callable[[httpx2.Request], httpx2.Response]
MakeService = Callable[..., ProvidersService]


def _handler(request: httpx2.Request) -> httpx2.Response:
    if request.headers.get("authorization") != f"Bearer {GOOD}":
        return httpx2.Response(401, json={"error": {"message": "Invalid API key"}})
    data = [
        {"id": "m1", "context_window": 8192},
        {"id": "m2", "context_length": 4096},
        {"id": "m3"},
        {"id": "m4", "context_window": "big"},
        {"name": "no id"},
    ]
    return httpx2.Response(200, json={"object": "list", "data": data})


@pytest.fixture
async def make_service(
    databases: Databases, keystore: MemoryKeyStore
) -> AsyncIterator[MakeService]:
    clients: list[httpx2.AsyncClient] = []

    def make(handler: Handler = _handler) -> ProvidersService:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        clients.append(client)
        return ProvidersService(databases, keystore, client)

    yield make
    for client in clients:
        await client.aclose()


HOSTED = ProviderCreate(name="groq", kind="hosted", base_url="https://api.groq.com/openai/v1/")


async def test_create_and_duplicate(make_service: MakeService) -> None:
    svc = make_service()
    provider = await svc.create_provider(HOSTED)
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert provider.id.startswith("prv_")
    with pytest.raises(DomainError) as err:
        await svc.create_provider(HOSTED)
    assert err.value.code == "provider_exists"


def test_base_url_with_credentials_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not carry credentials"):
        ProviderCreate(name="x", kind="hosted", base_url="https://u:p@x.io/v1")


async def test_add_key_stores_only_in_keystore(
    make_service: MakeService, keystore: MemoryKeyStore, paths: DataPaths
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    provider = await svc.add_key("groq", f"  {GOOD}  ")
    assert [k.hint for k in provider.keys] == ["••••3456"]
    assert list(keystore.data.values()) == [GOOD]
    files = (paths.app_db, paths.app_db.with_name("app.db-wal"))
    stored = b"".join(p.read_bytes() for p in files if p.exists())
    assert GOOD.encode() not in stored
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", GOOD)
    assert err.value.code == "key_exists"


async def test_rejected_key_is_not_stored(
    make_service: MakeService, keystore: MemoryKeyStore
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", "wrong")
    assert err.value.code == "key_rejected"
    assert keystore.data == {}


async def test_unreachable_provider_error_hides_the_key(make_service: MakeService) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot connect while sending {GOOD}")

    svc = make_service(boom)
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", GOOD)
    assert err.value.code == "provider_unreachable"
    assert GOOD not in err.value.message


async def test_remove_key_and_delete_provider_clean_keystore(
    make_service: MakeService, keystore: MemoryKeyStore
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    await svc.add_key("groq", GOOD)
    provider = await svc.remove_key("groq", 1)
    assert provider.keys == []
    assert keystore.data == {}
    await svc.add_key("groq", GOOD)
    await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    await svc.delete_provider("groq")
    assert keystore.data == {}
    assert await svc.list_models() == []


async def test_remote_models_and_context_window(make_service: MakeService) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    await svc.add_key("groq", GOOD)
    remote = await svc.list_remote_models("groq")
    assert [(m.id, m.context_window) for m in remote] == [
        ("m1", 8192),
        ("m2", 4096),
        ("m3", None),
        ("m4", None),
    ]
    entry = await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    assert entry.address == "groq:m1"
    assert entry.context_window == 8192
    assert entry.availability == "untested"
    manual = await svc.add_model(
        ModelCreate(provider="groq", wire_model_id="m3", context_window=2048)
    )
    assert manual.context_window == 2048
    updated = await svc.set_context_window("groq:m3", 16000)
    assert updated.context_window == 16000
    with pytest.raises(DomainError) as err:
        await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    assert err.value.code == "model_exists"


async def test_answer_that_is_not_a_model_list(make_service: MakeService) -> None:
    def not_a_list(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=["not", "a", "list"])

    svc = make_service(not_a_list)
    await svc.create_provider(
        ProviderCreate(name="local", kind="self_hosted", base_url="http://localhost:11434/v1")
    )
    with pytest.raises(DomainError) as err:
        await svc.list_remote_models("local")
    assert err.value.code == "provider_error"


async def test_hosted_provider_without_key_cannot_list(make_service: MakeService) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.list_remote_models("groq")
    assert err.value.code == "no_keys"


async def test_detect_and_import_env_keys(make_service: MakeService) -> None:
    svc = make_service()
    env = {
        "GROQ_API_KEY": GOOD,
        "GROQ_API_KEY_1": "gsk_second_key_0000",
        "GROQ_API_KEY_3": "gap",
    }
    found = await svc.detect_env_keys(env)
    assert [(d.env_var, d.provider_name, d.provider_exists) for d in found] == [
        ("GROQ_API_KEY", "groq", False),
        ("GROQ_API_KEY_1", "groq", False),
    ]
    assert all(GOOD not in json.dumps(d.model_dump()) for d in found)
    provider = await svc.import_env_key("GROQ_API_KEY", env)
    assert provider.name == "groq"
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert len(provider.keys) == 1
    with pytest.raises(DomainError) as err:
        await svc.import_env_key("SOMETHING_ELSE", env)
    assert err.value.code == "unknown_env_key"
```

`tests/conftest.py` is importable as `tests.conftest` because `tests/__init__.py` exists (backend-core plan, Task 1).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/providers -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mlops_agents.providers'`.

- [ ] **Step 3: Implement** `backend/src/mlops_agents/providers/models.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from mlops_agents.credentials.scrub import base_url_problem

ProviderKind = Literal["hosted", "self_hosted"]
ProviderStatus = Literal["ok", "needs_attention"]
Availability = Literal["untested", "verified", "needs_attention"]


class ProviderCreate(BaseModel):
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,39}$",
        description="Lowercase letters, digits and dashes; used in model addresses.",
    )
    kind: ProviderKind
    base_url: str

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        problem = base_url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class KeyHint(BaseModel):
    position: int
    hint: str


class Provider(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    status: ProviderStatus
    status_detail: str | None
    keys: list[KeyHint]


class ModelCreate(BaseModel):
    provider: str
    wire_model_id: str = Field(min_length=1)
    context_window: int | None = Field(default=None, gt=0)


class ModelEntry(BaseModel):
    address: str
    provider_id: str
    provider_name: str
    wire_model_id: str
    context_window: int | None
    tool_calling: bool
    streaming: bool
    availability: Availability
    availability_detail: str | None
    verified_at: str | None


class RemoteModel(BaseModel):
    id: str
    context_window: int | None


class DetectedKey(BaseModel):
    env_var: str
    provider_name: str
    base_url: str
    hint: str
    provider_exists: bool
```

`backend/src/mlops_agents/providers/service.py`:

```python
from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import Mapping
from itertools import count

import httpx2
from pydantic import BaseModel, ValidationError
from sqlalchemy import RowMapping, delete, func, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore
from mlops_agents.credentials.scrub import error_text, mask_key, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.providers.models import (
    DetectedKey,
    KeyHint,
    ModelCreate,
    ModelEntry,
    Provider,
    ProviderCreate,
    RemoteModel,
)
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

    # Environment keys

    async def detect_env_keys(self, env: Mapping[str, str] | None = None) -> list[DetectedKey]:
        source = os.environ if env is None else env
        existing = {p.name for p in await self.list_providers()}
        found: list[DetectedKey] = []
        for base, (provider_name, base_url) in KNOWN_ENV_KEYS.items():
            names = [base]
            for i in count(1):
                if not source.get(f"{base}_{i}"):
                    break
                names.append(f"{base}_{i}")
            for var in names:
                value = source.get(var)
                if value:
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
```

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run pytest -W error && uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/mlops_agents/providers backend/tests/providers
git commit -m "feat(providers): add providers registry with keys, models and env key import"
```

---

### Task 2: Fake provider, verification, error classes, bridge

**Files:**
- Create: `backend/tests/fakes/__init__.py` (empty), `backend/tests/fakes/fake_openai.py`, `backend/src/mlops_agents/providers/errors.py`, `bridge.py`, `verification.py`
- Modify: `backend/src/mlops_agents/providers/service.py` (add `verify_model`), `backend/tests/conftest.py` (add the `model_http` and `fake_openai` fixtures), `backend/src/mlops_agents/credentials/scrub.py` (`error_text` also withholds `httpx2.InvalidURL`)
- Test: `backend/tests/providers/test_errors.py`, `test_verification.py`, `test_verify_service.py`; add a test to `backend/tests/credentials/test_scrub.py`

**Interfaces:**
- Consumes: `ProvidersService` (Task 1), `scrub_values`, `error_text` (backend-core plan, Task 4).
- Changes: `error_text` treats `httpx2.InvalidURL` like `httpx.InvalidURL`, because model calls now raise `httpx2` errors and their URL-parse messages can quote part of a key.
- Produces:
  - `tests/fakes/fake_openai.py`: `FakeState` (fields `required_key: str | None`, `rate_limit_remaining: int`, `server_error_remaining: int`, `auth_fail: bool`, `usage_in_stream: bool`, `requests: list[dict[str, Any]]`, method `reset()`), `create_fake_app(state) -> FastAPI`, `RunningFake(base_url: str, state: FakeState)`, `start_fake_server(state, port=0) -> RunningFake`; runnable as `python -m tests.fakes.fake_openai --port N`. Models: `fake-tools` (calls the first offered tool, then answers from the tool result), `fake-no-tools` (ignores tools), `fake-slow` (40 words, 50 ms apart), each with context window 32768 except `fake-no-tools` (8192).
  - `conftest.py` fixtures `fake_openai -> RunningFake` (one server per test session, state reset per test) and `model_http -> httpx2.AsyncClient` (a client closed after the test; stands in for the app's shared client).
  - `errors.py`: `class ProviderErrorKind(StrEnum)` (`AUTH`, `QUOTA`, `RATE_LIMIT`, `TRANSIENT`, `UNREACHABLE`, `BAD_REQUEST`, `UNKNOWN`), `class ProviderCallError(Exception)` (`kind`, `message`, `retry_after: float | None`), `classify(exc: BaseException, secrets: Iterable[str] = ()) -> ProviderCallError`.
  - `bridge.py`: `KEYLESS: str`, `build_chat_model(base_url, api_key: str | None, wire_model_id, *, http: httpx2.AsyncClient, timeout=120.0) -> ChatOpenAI`, frozen dataclass `ResolvedModel(address, provider_id, provider_name, base_url, wire_model_id, context_window: int, keys: tuple[str, ...])` with `build(http: httpx2.AsyncClient, key_index: int = 0) -> ChatOpenAI`.
  - `verification.py`: `PROBE_TOOL`, `ProbeResult(tool_calling, streaming, detail)`, `async probe_model(base_url, api_key, wire_model_id, *, http: httpx2.AsyncClient, timeout=60.0) -> ProbeResult` (raises `ProviderCallError`).
  - `ProvidersService.verify_model(address) -> ModelEntry`.

- [ ] **Step 1: Write the fake server** `backend/tests/fakes/fake_openai.py`:

```python
"""A fake OpenAI-compatible server: deterministic, streaming, with scripted failures.

Used by backend tests (fixture `fake_openai`) and by the end-to-end test, which registers it
as an ordinary self-hosted provider. Run it alone with: python -m tests.fakes.fake_openai --port N
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

MODELS: dict[str, int] = {"fake-tools": 32768, "fake-no-tools": 8192, "fake-slow": 32768}
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "Read": {"file_path": "README.md"},
    "report_capabilities": {"ok": True},
}
SLOW_TEXT = " ".join(f"word{i}" for i in range(40))


@dataclass
class FakeState:
    required_key: str | None = None
    rate_limit_remaining: int = 0
    server_error_remaining: int = 0
    auth_fail: bool = False
    usage_in_stream: bool = True
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def reset(self) -> None:
        self.required_key = None
        self.rate_limit_remaining = 0
        self.server_error_remaining = 0
        self.auth_fail = False
        self.usage_in_stream = True
        self.requests.clear()


def _error(status: int, message: str, code: str, headers: dict[str, str] | None = None) -> Response:
    body = {"error": {"message": message, "type": "fake_error", "code": code}}
    return JSONResponse(body, status_code=status, headers=headers)


def _chunk(model: str, delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _reply(
    model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """(text, name of the tool to call or None)."""
    last = messages[-1]
    if model == "fake-slow":
        return SLOW_TEXT, None
    if model == "fake-no-tools" or not tools:
        return "Hello from the fake model.", None
    if last.get("role") == "tool":
        content = str(last.get("content") or "").strip()
        first_line = content.splitlines()[0] if content else "(empty)"
        return f"Done. First line: {first_line}", None
    return "", str(tools[0]["function"]["name"])


async def _stream(
    state: FakeState,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    usage: bool,
) -> AsyncIterator[str]:
    text, tool = _reply(model, messages, tools)
    yield _chunk(model, {"role": "assistant", "content": ""})
    if tool is not None:
        args = json.dumps(TOOL_ARGS.get(tool, {}))
        half = len(args) // 2
        call_id = f"call_{len(state.requests)}"
        first = {
            "index": 0,
            "id": call_id,
            "type": "function",
            "function": {"name": tool, "arguments": args[:half]},
        }
        yield _chunk(model, {"tool_calls": [first]})
        yield _chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]})
        finish = "tool_calls"
    else:
        for i, word in enumerate(text.split(" ")):
            if model == "fake-slow":
                await asyncio.sleep(0.05)
            yield _chunk(model, {"content": word if i == 0 else " " + word})
        finish = "stop"
    yield _chunk(model, {}, finish)
    if usage:
        tail: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        yield f"data: {json.dumps(tail)}\n\n"
    yield "data: [DONE]\n\n"


def create_fake_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    def authorized(request: Request) -> bool:
        if state.auth_fail:
            return False
        return state.required_key is None or (
            request.headers.get("authorization") == f"Bearer {state.required_key}"
        )

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        data = [{"id": m, "object": "model", "context_window": c} for m, c in MODELS.items()]
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not authorized(request):
            return _error(401, "Invalid API key", "invalid_api_key")
        body: dict[str, Any] = await request.json()
        state.requests.append(body)
        if state.rate_limit_remaining > 0:
            state.rate_limit_remaining -= 1
            return _error(429, "Rate limit reached", "rate_limit_exceeded", {"retry-after": "0"})
        if state.server_error_remaining > 0:
            state.server_error_remaining -= 1
            return _error(503, "The model is overloaded", "overloaded")
        model = str(body["model"])
        if model not in MODELS:
            return _error(404, f"Unknown model {model}", "model_not_found")
        options: dict[str, Any] = body.get("stream_options") or {}
        usage = bool(options.get("include_usage")) and state.usage_in_stream
        stream = _stream(state, model, body["messages"], body.get("tools") or [], usage)
        return StreamingResponse(stream, media_type="text/event-stream")

    return app


@dataclass
class RunningFake:
    base_url: str
    state: FakeState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_server(state: FakeState, port: int = 0) -> RunningFake:
    port = port or _free_port()
    config = uvicorn.Config(
        create_fake_app(state), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake OpenAI server did not start within 10 seconds")
        time.sleep(0.02)
    return RunningFake(f"http://127.0.0.1:{port}/v1", state, server, thread)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OpenAI-compatible server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(create_fake_app(FakeState()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
```

Add to `backend/tests/conftest.py`. At the top, with the other imports:

```python
import httpx2

from tests.fakes.fake_openai import FakeState, RunningFake, start_fake_server
```

At the end of the file:

```python
@pytest.fixture
async def model_http() -> AsyncIterator[httpx2.AsyncClient]:
    """The shared client the app passes to model calls, closed after the test."""
    async with httpx2.AsyncClient() as client:
        yield client


@pytest.fixture(scope="session")
def _fake_openai_server() -> Iterator[RunningFake]:
    running = start_fake_server(FakeState())
    yield running
    running.stop()


@pytest.fixture
def fake_openai(_fake_openai_server: RunningFake) -> RunningFake:
    _fake_openai_server.state.reset()
    return _fake_openai_server
```

`AsyncIterator` and `Iterator` are already imported there. Run `uv run ruff check --fix` to sort the imports.

- [ ] **Step 2: Write the failing tests**

`backend/tests/providers/test_errors.py`:

```python
import httpx2
import openai

from mlops_agents.providers.errors import ProviderErrorKind, classify

REQUEST = httpx2.Request("POST", "https://x.io/v1/chat/completions")


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, request=REQUEST, headers=headers)


def test_auth() -> None:
    err = openai.AuthenticationError("bad key", response=_response(401), body=None)
    assert classify(err).kind is ProviderErrorKind.AUTH


def test_rate_limit_with_retry_after() -> None:
    err = openai.RateLimitError(
        "slow down", response=_response(429, {"retry-after": "3"}), body=None
    )
    result = classify(err)
    assert result.kind is ProviderErrorKind.RATE_LIMIT
    assert result.retry_after == 3.0


def test_quota() -> None:
    err = openai.RateLimitError(
        "You exceeded your current quota",
        response=_response(429),
        body={"code": "insufficient_quota"},
    )
    assert classify(err).kind is ProviderErrorKind.QUOTA


def test_overloaded_is_transient() -> None:
    err = openai.InternalServerError("high demand", response=_response(503), body=None)
    assert classify(err).kind is ProviderErrorKind.TRANSIENT


def test_connection_error_is_unreachable() -> None:
    assert (
        classify(openai.APIConnectionError(request=REQUEST)).kind is ProviderErrorKind.UNREACHABLE
    )


def test_wrapped_error_is_found_through_the_cause_chain() -> None:
    inner = openai.AuthenticationError("bad key", response=_response(401), body=None)
    try:
        try:
            raise inner
        except openai.AuthenticationError as exc:
            raise RuntimeError("wrapper") from exc
    except RuntimeError as outer:
        assert classify(outer).kind is ProviderErrorKind.AUTH


def test_message_hides_secrets() -> None:
    err = RuntimeError("failed with key gsk_abcdef123456")
    result = classify(err, ["gsk_abcdef123456"])
    assert result.kind is ProviderErrorKind.UNKNOWN
    assert "gsk_abcdef123456" not in result.message


def test_dropped_stream_is_unreachable() -> None:
    err = httpx2.RemoteProtocolError("peer closed connection without sending complete message body")
    assert classify(err).kind is ProviderErrorKind.UNREACHABLE
```

`backend/tests/providers/test_verification.py`:

```python
import httpx2
import pytest

from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind
from mlops_agents.providers.verification import probe_model
from tests.fakes.fake_openai import RunningFake


async def test_tool_calling_model_passes(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    result = await probe_model(fake_openai.base_url, None, "fake-tools", http=model_http)
    assert result.tool_calling
    assert result.streaming
    assert result.detail is None
    sent = fake_openai.state.requests[-1]
    assert sent["stream"] is True
    assert sent["tools"][0]["function"]["name"] == "report_capabilities"


async def test_model_that_ignores_tools_is_reported(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    result = await probe_model(fake_openai.base_url, None, "fake-no-tools", http=model_http)
    assert not result.tool_calling
    assert result.detail is not None
    assert "tool" in result.detail


async def test_missing_usage_in_stream_still_passes(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.usage_in_stream = False
    result = await probe_model(fake_openai.base_url, None, "fake-tools", http=model_http)
    assert result.tool_calling


async def test_bad_key_is_an_auth_error(
    fake_openai: RunningFake, model_http: httpx2.AsyncClient
) -> None:
    fake_openai.state.required_key = "right"
    with pytest.raises(ProviderCallError) as err:
        await probe_model(fake_openai.base_url, "wrong-key-value", "fake-tools", http=model_http)
    assert err.value.kind is ProviderErrorKind.AUTH
    assert "wrong-key-value" not in err.value.message
```

`backend/tests/providers/test_verify_service.py`:

```python
import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def _service(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    http: httpx2.AsyncClient,
) -> ProvidersService:
    svc = ProvidersService(databases, keystore, http)
    await svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    return svc


async def test_verify_records_traits(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    entry = await svc.add_model(ModelCreate(provider="fake", wire_model_id="fake-tools"))
    assert entry.context_window == 32768
    verified = await svc.verify_model("fake:fake-tools")
    assert verified.availability == "verified"
    assert verified.tool_calling
    assert verified.streaming
    assert verified.verified_at is not None


async def test_failed_verification_marks_the_model(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    svc = await _service(databases, keystore, fake_openai, model_http)
    await svc.add_model(ModelCreate(provider="fake", wire_model_id="fake-tools"))
    fake_openai.state.auth_fail = True
    with pytest.raises(DomainError) as err:
        await svc.verify_model("fake:fake-tools")
    assert err.value.code == "verification_failed"
    entry = await svc.require_model("fake:fake-tools")
    assert entry.availability == "needs_attention"
```

Add to `backend/tests/credentials/test_scrub.py` (imports `from types import ModuleType`, `import httpx`, `import httpx2`, and `error_text` from `mlops_agents.credentials.scrub`):

```python
@pytest.mark.parametrize("module", [httpx, httpx2])
def test_error_text_withholds_url_parse_errors(module: ModuleType) -> None:
    try:
        module.Request("GET", "http://host:gsk_secret_value/v1")
    except Exception as exc:
        assert "gsk_secret_value" not in error_text(exc)
        assert "withheld" in error_text(exc)
    else:
        pytest.fail("the URL should not parse")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/providers tests/credentials/test_scrub.py -v`
Expected: FAIL with `ModuleNotFoundError` for `mlops_agents.providers.errors` and `verification`, and `test_error_text_withholds_url_parse_errors[httpx2]` fails.

- [ ] **Step 4: Implement**

In `backend/src/mlops_agents/credentials/scrub.py`, `error_text` withholds both URL-parse error classes:

```python
    import httpx
    import httpx2

    if isinstance(exc, httpx.InvalidURL | httpx2.InvalidURL):
```

`backend/src/mlops_agents/providers/errors.py`:

```python
from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum

import httpx2
import openai

from mlops_agents.credentials.scrub import error_text, scrub_values


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    UNREACHABLE = "unreachable"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


class ProviderCallError(Exception):
    """A failed call to a model provider, classified. `message` is safe to show."""

    def __init__(
        self, kind: ProviderErrorKind, message: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """`exc` and its causes. langchain-openai wraps openai errors, so the type is in the chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _kind(exc: BaseException) -> tuple[ProviderErrorKind, float | None]:
    for e in _chain(exc):
        if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
            return ProviderErrorKind.AUTH, None
        if isinstance(e, openai.RateLimitError):
            body = str(e.body).lower() if e.body is not None else ""
            if "quota" in str(e).lower() or "insufficient_quota" in body:
                return ProviderErrorKind.QUOTA, None
            return ProviderErrorKind.RATE_LIMIT, _retry_after(e.response)
        if isinstance(e, openai.APIConnectionError | httpx2.TransportError):
            return ProviderErrorKind.UNREACHABLE, None
        if isinstance(e, openai.APIStatusError):
            if e.status_code >= 500:
                return ProviderErrorKind.TRANSIENT, None
            return ProviderErrorKind.BAD_REQUEST, None
    return ProviderErrorKind.UNKNOWN, None


def classify(exc: BaseException, secrets: Iterable[str] = ()) -> ProviderCallError:
    kind, retry_after = _kind(exc)
    return ProviderCallError(kind, scrub_values(error_text(exc), secrets), retry_after)
```

`backend/src/mlops_agents/providers/bridge.py`:

```python
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
```

`backend/src/mlops_agents/providers/verification.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2
from langchain_core.messages import AIMessageChunk, HumanMessage

from mlops_agents.providers.bridge import build_chat_model
from mlops_agents.providers.errors import classify

PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_capabilities",
        "description": "Call this tool to confirm that you can call tools.",
        "parameters": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    },
}
PROBE_PROMPT = "Call the report_capabilities tool with ok set to true. Do not answer with text."


@dataclass(frozen=True)
class ProbeResult:
    tool_calling: bool
    streaming: bool
    detail: str | None


async def probe_model(
    base_url: str,
    api_key: str | None,
    wire_model_id: str,
    *,
    http: httpx2.AsyncClient,
    timeout: float = 60.0,  # noqa: ASYNC109 -- passed to the OpenAI client, which applies it per request
) -> ProbeResult:
    """One real streamed request with a tool, the way agents call models."""
    model = build_chat_model(base_url, api_key, wire_model_id, http=http, timeout=timeout)
    # langchain-openai leaves bind_tools' keyword arguments untyped.
    llm = model.bind_tools([PROBE_TOOL])  # pyright: ignore[reportUnknownMemberType]
    chunks = 0
    merged: AIMessageChunk | None = None
    try:
        async for chunk in llm.astream([HumanMessage(PROBE_PROMPT)]):
            chunks += 1
            if isinstance(chunk, AIMessageChunk):
                merged = chunk if merged is None else merged + chunk
    except Exception as exc:
        raise classify(exc, [api_key] if api_key else []) from None
    calls = merged.tool_calls if merged is not None else []
    tool_calling = any(call["name"] == "report_capabilities" for call in calls)
    streaming = chunks >= 2
    if not tool_calling:
        detail = "The model did not call the test tool. Agents need a model that calls tools."
    elif not streaming:
        detail = "The provider did not stream the answer. Agents need streaming."
    else:
        detail = None
    return ProbeResult(tool_calling, streaming, detail)
```

Add to `ProvidersService` in `service.py` (with imports `from mlops_agents.providers.errors import ProviderCallError` and `from mlops_agents.providers.verification import probe_model`):

```python
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
```

- [ ] **Step 5: Run tests and checks**

Run: `cd backend && uv run pytest -W error && uv run ruff check && uv run ruff format --check && uv run pyright`
Expected: all pass, with no unclosed-socket warnings.

- [ ] **Step 6: Live check against a real provider** (marker `live`, not run in CI) — add `backend/tests/providers/test_live.py`:

```python
import os

import httpx2
import pytest

from mlops_agents.providers.verification import probe_model

pytestmark = pytest.mark.live


async def test_groq_streams_tool_calls() -> None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        pytest.skip("set GROQ_API_KEY to run this live test")
    async with httpx2.AsyncClient() as http:
        result = await probe_model(
            "https://api.groq.com/openai/v1", key, "qwen/qwen3.8-27b", http=http
        )
    assert result.tool_calling
    assert result.streaming
```

Run: `cd backend && GROQ_API_KEY=... uv run pytest -m live tests/providers/test_live.py -v`
Expected: PASS (verified on 2026-10-16 with this model).

- [ ] **Step 7: Commit**

```bash
git add backend/src/mlops_agents/providers backend/src/mlops_agents/credentials/scrub.py backend/tests/providers backend/tests/fakes backend/tests/conftest.py backend/tests/credentials/test_scrub.py
git commit -m "feat(providers): add model verification, error classification and the LangChain bridge"
```
