> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Embedders Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the stage overview first: `docs/superpowers/plans/drafts/2026-10-28-stage-3-overview.md`.

**Goal:** the AI providers registry holds two kinds of model, chat and embedder (spec §3):
- an embedder is added, verified (one `/embeddings` call that records its dimensions), and chosen as the app default or a project override;
- every picker shows only models of its type;
- the `Embedders` service hands the Knowledge store an `Embedder` that uses the key failover and never stalls a turn.

**Branch:** `plan/stage-3-embedders`. **Consumes:** I14 as contracts task 1 writes it in `providers/embedder.py` (`ModelType`, `EmbedderUnavailable`, `Embedder`, `EmbedderStatus`, `Embedders`) and the `models.model_type` and `models.dimensions` columns in `app.db`; the `ModelEntry` and `ModelCreate` fields are this plan's (`providers/models.py` is embedders'), as is the implementation; I21 (`KeyFailover.call(..., wait=False)`, `AllKeysExhausted`, `RateLimited`, `KeyState`; rate-limits task 1). **Produces:** `Embedders` (I14) from task 1 (early PR), the embedder settings routes, the type-aware model pickers.

**Read first:** spec §3 and §14; `backend/src/mlops_agents/providers/{models,service,verification,bridge,errors}.py`; `backend/src/mlops_agents/agents/settings.py` (`_check_model`, `_require_usable_model`); `frontend/src/pages/providers/`; the stage 3 overview's I14, I21 and "Files shared"; `backend/src/mlops_agents/providers/failover.py` as rate-limits task 1 wrote it (its constructor, for the tests).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Model types, the embedder verify step, `Embedder` and `Embedders` | contracts task 1, rate-limits task 1 |
| 2 | Embedder settings routes | task 1 |
| 3 | Type-aware model registry UI | task 2 |
| 4 | Embedder settings UI and the end-to-end flow | tasks 2 and 3 |

Task 1 gets an early PR, from `plan/stage-3-embedders-task1`: the knowledge plan builds on `Embedders`.

---

### Task 1: Model types, the embedder verify step, `Embedder` and `Embedders`

**Files:**
- `backend/tests/fakes/fake_openai.py`: the `/embeddings` route and its own functions (`embeddings`, `fake_embedding`), plus a shared helper for the scripted failures (below). Rate-limits edits other parts of this file (429 and 402 scripts); keep this plan's additions in separate functions and a separate block of `FakeState` fields.
- `backend/src/mlops_agents/providers/bridge.py` (`build_embeddings`).
- `backend/src/mlops_agents/providers/verification.py` (`probe_embedder`, `EmbedderProbe`).
- `backend/src/mlops_agents/providers/models.py` (`ModelEntry.model_type: ModelType` and `dimensions: int | None`, `ModelCreate.model_type: ModelType = "chat"`, `ModelEntry.problem` per type, `ModelEntry.problem_for`). Contracts already added `Provider.cooldown_minutes` there (its only line): do not touch it. Rate-limits adds its key-failover lines to `providers/service.py`: keep each plan's lines apart there.
- `backend/src/mlops_agents/providers/service.py` (`add_model`, `_model`, `verify_model`, `record_verification`).
- New `backend/src/mlops_agents/providers/provider_embedder.py` (`ProviderEmbedder`; `providers/embedder.py` is contracts' file of I14 types), new `providers/embedders.py` (`EmbeddersService`, the settings key).
- `backend/src/mlops_agents/agents/settings.py` (chat models only).
- `backend/src/mlops_agents/api/services.py` (one line: the `embedders` service).
- `frontend/openapi.json`, regenerated (never edited by hand): `ModelEntry` and `ModelCreate` change `/api/models`, and both `test_committed_contract_matches_a_fresh_export` and the Backend CI step `openapi_export --check` fail on a stale file.
- Tests: additions to `tests/fakes/test_fake_openai.py`, `tests/providers/test_embedder_verify.py`, `tests/providers/test_provider_embedder.py`, `tests/providers/test_embedders.py`, additions to `tests/providers/test_model_problem.py` and `tests/agents/test_settings.py`.

**Interfaces:**
- Consumes I14 (as in the header, including `EmbedderStatus` and `Embedders.status`) and I21: `KeyFailover.call(provider, model, attempt, cooldown_minutes=provider.cooldown_minutes, wait=False)` where `attempt(key_index: int | None)` does one call and raises the provider's raw exception (the failover classifies it); `AllKeysExhausted.keys: list[KeyState]`; `RateLimited`.
- Produces `ProviderEmbedder` (implements `Embedder`; the I14 types are imported from `providers/embedder.py`), `EmbeddersService` (implements `Embedders`), and these, which are this plan's own:
  - `ModelEntry.problem_for(wanted: ModelType) -> str | None`: the wrong-type message when `model_type != wanted`, else `problem`.
  - `EmbeddersService.status(project_id: str) -> EmbedderStatus` (I14: `{configured: bool, address: str | None, dimensions: int | None, problem: str | None}`): `configured` is whether an embedder is chosen (the project override, else the app default); `address` the chosen model's address (even when unusable); `dimensions` that model's verified dimensions when usable; `problem` why it is not usable (null when usable or none chosen). `EmbeddersService.default_status()` gives the same for the app default alone (used by the app settings route; not in the protocol, which only knowledge consumes).
  - `EmbeddersService.set_default(address: str | None) -> None` and `set_override(project_id: str, address: str | None) -> None`, and `chosen_default()` / `chosen_override(project_id)` returning the stored address (the settings routes show what is stored apart from what is in effect).
  - One settings key, `EMBEDDER_KEY = "embedder.model"` (dotted, like `agent.<name>.model`), defined once in `embedders.py` and used in both tables: the `settings` table of `app.db` (`app_settings` in `tables.py`) for the app default, `project_settings` of `project.db` for the override. The value is a model address.

**Implementation notes:**
- **Fake `/embeddings`** (`POST /v1/embeddings`, body `{model, input: str | list[str]}`; answer `{object: "list", data: [{object: "embedding", index, embedding}], model, usage}`):
  - Models: `EMBEDDING_MODELS = {"fake-embed": 16, "fake-embed-wide": 32}` (name to dimensions), listed by `GET /v1/models` next to the chat models (no test may count the entries of `MODELS` or of the `/v1/models` answer; tests look models up by name, as `test_fake_openai.py` and `test_providers_routes.py` already do); an unknown model is 404 `model_not_found`.
  - `fake_embedding(text, dimensions) -> list[float]` is public, deterministic and bag-of-words (a hash of each lowercased word picks a component and a sign; the vector is L2-normalized), so texts sharing words are closer: the knowledge plan's tests depend on this, and use the same function to compute expectations. Use `hashlib.sha256`, never `hash()` (it changes per process).
  - Scripted failures: `/embeddings` honors the same `FakeState` fields as `chat` (`required_key` and `auth_fail` through the existing `authorized()`, `rate_limit_remaining`, `server_error_remaining`), through its own function `embeddings_failure(state, request) -> Response | None`. `chat` is not edited (rate-limits changes its 429 and 402 scripts in the same file); if rate-limits' scripts are already on `main` when this task starts, `embeddings_failure` calls their shared helper instead of re-reading the fields.
  - One new field, `embedding_wrong_dimensions: int | None` (answers vectors of that size, to test the mismatch check), reset in `reset()`. Embedding requests are recorded in the existing `requests` and `request_headers` lists.
- **`build_embeddings(base_url, api_key, wire_model_id, *, http, timeout) -> OpenAIEmbeddings`** in `bridge.py`, next to `build_chat_model` (the one place that builds provider clients): `http_async_client=http`, `max_retries=0`, `api_key=SecretStr(api_key or KEYLESS)`, and, costly if missed:
  - `check_embedding_ctx_length=False`. By default langchain-openai tokenizes with tiktoken and sends token IDs, which Ollama, vLLM, LM Studio and Gemini's compatibility layer do not accept. Raw strings go over the wire.
  - `chunk_size=EMBED_BATCH` (a constant in `bridge.py`, 64): some providers cap the batch below langchain's default of 1000.
  - Never send `dimensions` (the model's native size is recorded by verify and checked on every call).
- **`probe_embedder(base_url, api_key, wire_model_id, *, http, timeout) -> EmbedderProbe`** (`EmbedderProbe` is a frozen dataclass `{dimensions: int}`): `aembed_query(EMBED_PROBE_TEXT)` (a constant, "a short sentence about model monitoring"); exceptions go through `classify(exc, secrets)` exactly as `probe_model` does (assumption: rate-limits keeps `classify`'s signature and the `UNKNOWN` kind in `providers/errors.py`); an answer with no vector, an empty vector or a non-finite value raises `ProviderCallError` of kind `UNKNOWN` with "The provider's /embeddings answer holds no usable vector."
- **Service.**
  - `add_model`: stores `model_type`; for an embedder, skips the context-window lookup (not used) and leaves `context_window` null; `tool_calling`, `streaming` stay 0 and `dimensions` null until verified.
  - `_model` reads `model_type` and `dimensions`.
  - `verify_model` branches on `entry.model_type`: chat as today; embedder calls `probe_embedder` with the first key (as chat verify does: a user-run check of the provider, not a turn, so it does not use the failover) and `record_verification(address, tool_calling=False, streaming=False, detail=None, dimensions=probe.dimensions)`. A failure marks `needs_attention` exactly as for chat.
  - `record_verification` gains `dimensions: int | None = None`; a chat verify writes null (a model re-added as a different type cannot keep a stale value: the type is fixed when the model is added, so this only matters for re-verification, which rewrites it).
  - The `models` table has `UNIQUE (provider_id, wire_model_id)`, so one model ID cannot be added as both types for one provider. Keep it: a provider's chat and embedding models have different IDs. A clash gets the existing `model_exists` error.
- **`ModelEntry.problem` per type** (still the one place the rule lives):
  - chat: unchanged;
  - embedder: `needs_attention` message as today; not verified or `dimensions is None` → "The embedder {address} has not passed verification (an embeddings call). Verify it in AI providers or choose another model."; no context-window rule.
  - `problem_for(wanted)`: wrong type → "{address} is an {embedder|chat} model, not a {wanted} model. Choose a {wanted} model." (a small `_TYPE_LABELS` mapping, no duplicated strings).
  - `agents/settings.py`: `_check_model` and `_require_usable_model` call `entry.problem_for("chat")` instead of `entry.problem`, so an embedder can never be assigned to an agent and an agent already pointing at one reports it as a problem. Nothing else in that file changes.
- **`ProviderEmbedder(address, dimensions, ...)`:**
  - Built by `EmbeddersService`, holds the `Provider`, the wire model ID, `KeyFailover`, `ProvidersService` (for `keys_for`) and the shared `http`.
  - `embed(texts)`: empty input returns `[]` with no call; otherwise `failover.call(provider, wire_model_id, attempt, cooldown_minutes=provider.cooldown_minutes, wait=False)` (an embedding never waits out a rate limit: it runs at turn start and in `SaveNote`). `attempt(key_index)` builds `build_embeddings(..., key)` with `key = None` when `key_index is None` (a provider with no keys: the failover calls `attempt(None)` once), else `keys[key_index]` where `keys = await ProvidersService.keys_for(provider.id)` (the same position-ordered list the failover's `key_index` and `provider.keys` follow), and awaits `aembed_documents(list(texts))`. `attempt` raises the raw exception and does nothing else: it neither classifies nor checks the answer (the failover would treat an exception raised there as a provider error, retry it or mark the key).
  - After `call` returns, `embed` checks the answer: as many vectors as texts, each of `self.dimensions` floats. A mismatch is an `EmbedderUnavailable` saying the model now answers N dimensions and to verify it again in AI providers (never a silent truncation: mixing sizes corrupts the vector table).
  - Mapping to `EmbedderUnavailable` (user-facing, scrubbed): `AllKeysExhausted` gives `"<provider>: no usable key for <model>. " + failover.describe_keys(exc.keys)` (`KeyFailover.describe_keys(states) -> str`, I21: the one wording, shared with the stall card); `RateLimited` says every key of the provider is rate-limited right now and search uses full text; any other exception (the failover re-raises what it does not handle, raw) goes through `classify(exc, keys)` and gives its `message`. Nothing else escapes `embed` except `asyncio.CancelledError`.
  - It never raises a stall: nothing here touches approvals or the turn graph.
- **`EmbeddersService(databases, providers, failover, http)`:**
  - `for_project(project_id)` (I14): reads the project override, else the app default; `None` when neither is set. The override wins even when unusable: it returns `None` rather than falling back to the default (a user who chose a self-hosted embedder for one project does not want its notes sent to a hosted default).
  - A chosen model that is gone, not an embedder, has a `problem`, or whose provider `needs_attention` or has no key (hosted) is "not usable": `for_project` returns `None` and `status().problem` says why (same messages as `_check_model`'s; the type message is `problem_for("embedder")`). Knowledge puts that text into `SearchResult.detail`.
  - `status` and `default_status` share one private resolver (address in, `EmbedderStatus` out); `for_project` is `status` plus building the embedder.
  - `set_default` / `set_override` accept `None` (clear) or an address that exists, is an embedder and has no `problem`; anything else is a `DomainError` 409 with `model_not_usable` and the `problem_for("embedder")` text, 404 `model_not_found` for an unknown address.
  - Models deleted later leave the setting; `status` explains it (as agent defaults do).
- **Wiring:** `Services.embedders: EmbeddersService`, one line in the dataclass, one in `build_services`, built after `providers` with the app's `KeyFailover` (rate-limits wires that single instance; reuse it, never create a second: the state must be shared).

**Tests (stop condition):**
- Fake server: `/embeddings` answers the right dimensions for each model, accepts a string and a list, is deterministic across calls and processes, ranks a text sharing words above one that does not, 401 without the key, 404 for a chat model name, honors `required_key`, `auth_fail`, `rate_limit_remaining` and `wrong_dimensions`; `/models` lists both kinds.
- Verify: an embedder verifies against the fake and records `dimensions` 16 with `tool_calling` and `streaming` false; `availability` is `verified`; `problem` is null without a context window; `fake-no-tools` as an embedder is not found (404 from the fake) and marks `needs_attention` with the scrubbed provider message; 401 marks `needs_attention` and the key never appears in the message; a chat model's verify is unchanged and `dimensions` stays null.
- The request that reaches the fake carries raw strings (`fake_openai.state.requests[0]["input"]` is a `str` or `list[str]`, never ints): this proves `check_embedding_ctx_length=False`. Batches larger than `EMBED_BATCH` split.
- `problem` and `problem_for`: an unverified embedder, a verified embedder with null dimensions, a verified embedder, a chat model asked as embedder, an embedder asked as chat. `agents/settings`: assigning an embedder to an agent is refused with `model_not_usable` (default and project override); an agent whose default is an embedder gets the problem text in `resolve`.
- `ProviderEmbedder`: the vectors equal `fake_embedding`; with the real `KeyFailover` and two keys, `rate_limit_remaining = 1` gives one 429 and the second key succeeds (the failover's own rules are rate-limits' tests; this one proves `attempt` is wired to the key index); a provider with no keys (self-hosted) is called once with `attempt(None)`; the failover is called with `wait=False` and `cooldown_minutes` equal to the provider's (a recording subclass); with `KeyFailover` subclasses whose `call` raises `AllKeysExhausted` or `RateLimited`: `EmbedderUnavailable` carrying the `describe_keys` text, or saying every key is rate-limited, and neither exception escapes; a permanent error (`auth_fail`) comes out of the failover raw and is classified into `EmbedderUnavailable` with the key value scrubbed; the dimension mismatch is raised after `call` returns (a recording failover sees one successful call, no retry, no key marked); the unreachable provider (stopped server) raises it with no secret in the message; `wrong_dimensions` raises it with the verify-again text; empty input makes no request; a cancelled call propagates cancellation.
- `EmbeddersService`: default only, override only, both (override wins), neither (`None`, `configured` false, no problem); an override that is not usable returns `None` and does not fall back; a deleted model, a chat model and an unverified embedder give `None` with `problem` set to their messages; `dimensions` is filled when usable; `set_default`/`set_override` validation and clearing; two projects have independent overrides.
- Full backend suite; checks.

Checks: `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest` (includes the openapi check), `uv run python -m mlops_agents.api.openapi_export --check`.

**PR:** open an early PR into `main` from `plan/stage-3-embedders-task1` (`Closes #<task issue>`).

---

### Task 2: Embedder settings routes

**Files:** new `backend/src/mlops_agents/api/routers/embedders.py` (routers are included automatically by `include_routers`), `frontend/openapi.json` (regenerated, never edited by hand; `schema.d.ts` is not committed, `npm run gen:api` builds it before lint, test and build), tests `tests/api/test_embedders_routes.py`.

**Interfaces:** Consumes task 1 (`EmbeddersService`, `EmbedderStatus`). Produces:
- `GET /api/settings/embedder` and `PUT /api/settings/embedder` (body `EmbedderChoice {address: str | None}`), both answering `EmbedderSettingView {chosen: str | None, status: EmbedderStatus}` for the app level (`chosen` is the stored default, `status` is `default_status()`);
- `GET /api/projects/{project_id}/settings/embedder` and `PUT` (same body), answering `EmbedderSettingView` with `chosen` the project override and `status` the one in effect from `status(project_id)` (so the UI can show "using the app default x" when `chosen` is null).

**Implementation notes:**
- Routes are thin: they call `services.embedders`; `ProjectDep` checks the project. `EmbedderChoice` forbids extra fields as the other bodies do.
- `EmbedderSettingView` is a route model defined in `embedders.py`; `EmbedderStatus` comes from I14.
- Regenerate `frontend/openapi.json` from the backend as the frontend commands say. If contracts' regenerated file is already on `main`, merge it first and regenerate; never merge the generated files by hand.

**Tests (stop condition):** anonymous requests get 401 (as `test_models_routes.py` does); default: set a verified embedder, read it back, clear it; refused for a chat model, an unverified embedder and an unknown address, each with its code and message; project: an override is returned as `chosen`, a project without one reports `chosen` null and the app default in `status.address`, an unknown project is 404, the override of one project does not show in another; deleting the chosen model leaves `chosen` set and `status.problem` filled, `dimensions` null; the OpenAPI export lists the four operations (extend the existing openapi test pattern if it asserts operations). Full suite; checks.

Checks: backend ruff, format, pyright, pytest; regenerate `openapi.json`, then `npm run lint` (it runs `gen:api`).

---

### Task 3: Type-aware model registry UI

**Files:** `frontend/src/lib/models.ts` (`usableModels`, `hostedEmbedderWarning`), `frontend/src/pages/providers/AddModelDialog.tsx`, `ModelsTable.tsx`, `AgentDefaults.tsx`, `frontend/src/pages/project-settings/AgentsSection.tsx`, their tests (`models.test.ts`, `AddModelDialog.test.tsx`, `ModelsTable.test.tsx`, `AgentDefaults.test.tsx`, and a new `frontend/src/pages/project-settings/AgentsSection.test.tsx`: none covers that section's picker yet).

**Interfaces:** Consumes the generated `ModelEntry` (`model_type`, `dimensions`) and `ModelCreate` (`model_type`) from `schema.d.ts`; no hand-written copies of them. Produces `usableModels(models: ModelEntry[], type: ModelType): ModelEntry[]` (the one filter every picker uses: `model_type === type && problem === null`), used by task 4's picker and the two agent pickers.

**Implementation notes:**
- `AddModelDialog`: a Type select (Chat model, Embedder; default Chat) above the Model field; the body sends `model_type`. Changing the type clears the model ID. Embedder help text: "Any OpenAI-compatible `/embeddings` endpoint works, hosted or self-hosted." The remote model list is the provider's full list (providers do not say which are embedders), so the combobox stays free-text with the list as suggestions.
- `ModelsTable`: a Type column; for an embedder, Tool calling and Streaming show "Not applicable", the Context window cell shows "Not applicable" (no Set button) and a Dimensions column shows the number, or "Not checked". The warning list uses `problem`, which is type-aware. Verify's label reads the same. Keep the table under about 400 lines: if the type-specific cells grow it past that, move the row to `ModelRow.tsx` (a move, no other change).
- `AgentDefaults` and `AgentsSection`: replace the inline `models.filter((m) => m.problem === null)` with `usableModels(models, "chat")`. An agent whose current model is an embedder shows "(not usable)" as for any unusable model (the server's `model_problem` carries the reason).
- `hostedEmbedderWarning(provider)`: "Note titles and text are sent to {provider} to compute their vectors." Shown by task 4 next to a hosted embedder choice.

**Tests (stop condition):** `usableModels` for each combination (type, problem, verified or not); the dialog sends `model_type: "embedder"` when chosen and `"chat"` by default, and resets the model ID when the type changes; the table renders chat and embedder rows (traits, Dimensions, no context-window button for the embedder, a verify call updates its Dimensions); both agent pickers list only usable chat models and never an embedder; an agent whose default is an embedder shows the not-usable entry. Full frontend suite; checks.

Checks: `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---

### Task 4: Embedder settings UI and the end-to-end flow

**Files:** new `frontend/src/components/EmbedderSelect.tsx` (+ test), new `frontend/src/lib/useEmbedderSettings.ts`, new `frontend/src/pages/app-settings/EmbedderSection.tsx` (+ test) and one line in `AppSettingsPage.tsx`, new `frontend/src/pages/project-settings/EmbedderSection.tsx` (+ test) and one line in `ProjectSettingsPage.tsx`, new `frontend/e2e/embedders.spec.ts` (`flow.spec.ts` belongs to onboarding).

**Interfaces:** Consumes task 2's routes and task 3's `usableModels` and `hostedEmbedderWarning`. Produces the two settings sections. The connections plan adds its own project settings section and line; neither edits the other's.

**Implementation notes:**
- `EmbedderSelect({value, onChange, models, providers, none})`: a Select of `usableModels(models, "embedder")` plus a "None" entry via `toSelectValue`/`fromSelectValue`; one component for both sections, so the filter and the hosted warning have one place. The label names its context ("Default embedder", "Embedder for this project"). The "None" text differs: app level "None: search uses full text only"; project level "Use the app default".
- `useEmbedderSettings(projectId?: string)`: the query for `GET` and the mutation for `PUT`, with the query keys `["embedder"]` and `["projects", id, "embedder"]`; a successful save invalidates both keys (an app default change alters every project's `status`).
- App section ("Embedder", in App settings): the select, the `status.problem` when set (an amber line, like model problems), and a sentence: "Notes are searched by meaning with this embedder. With none set, search uses full text only." Changing it does not re-embed here: that is the knowledge plan's job; say nothing about re-embedding in the text until it exists.
- Project section ("Embedder", in Project settings): the override select (spec §12 places it inside the Connections section; it is its own file here because connections owns that section's file), and a line "Using {status.address}" or "Using full text only{: problem}" from the status.
- The hosted warning shows when the chosen model's provider is hosted.

**Tests (stop condition):**
- Component tests with mocked API: the select lists only usable embedders (never a chat model, an unverified embedder, or one with a problem); choosing sends the `PUT` with the address and "None" sends null; a refused choice shows the server's message; the problem line appears when `status.problem` is set; the hosted warning shows for a hosted provider only; the project section reports the app default as the one in use; saving the app default refreshes the project section.
- **Playwright e2e** (`embedders.spec.ts`, uses the fake provider the other specs start): register the fake as a self-hosted provider, add `fake-embed` as an embedder, verify it (the row shows Dimensions 16), set it as the app default, and see it in a project's settings as the one in use; add `fake-tools` as a chat model in the same run and check it is absent from the embedder select and `fake-embed` absent from an agent's model select; override it for the project with `fake-embed-wide` (verified first), then clear the override.
- Full frontend suite and e2e; checks.

Checks: `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e`; backend ruff, format, pyright, pytest unchanged and green; CI green on the plan branch (Backend with its Windows job, Frontend, E2E) before the plan PR opens.
