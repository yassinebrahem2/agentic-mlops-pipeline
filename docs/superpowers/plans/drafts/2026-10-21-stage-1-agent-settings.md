> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Agent Settings Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. The task is one PR into `main` (`AGENTS.md` rule 5). It lists its files, the interfaces it consumes and produces, its tests (the stop condition) and the checks that prove it works.

**Goal:** Which model each agent uses and how many steps it may take: a default model per agent at app level, a per-project override of the model and of the step limit, and one `resolve` call that gives a turn its model or a clear message saying what to choose.

**Branch:** `plan/stage-1-agent-settings`. **Consumes:** merged code only. **Produces:** I2 of the stage overview, which agent-runtime and api-resources consume.

**Read first:** the stage overview (global constraints, "Interfaces between plans" I2), spec section 3 ("Models", "Verification", "Assignment") and section 5 ("Limits").

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Agent settings service | merged code |

One task: the whole plan is I2, and the plans that consume it need all of it at once.

---

### Task 1: Agent settings service

**Files:**
- Create: `backend/src/mlops_agents/agents/settings.py`
- Test: `backend/tests/agents/test_settings.py`

**Interfaces:**
- Consumes: `Databases`, the `agent_model_defaults` (app.db) and `project_settings` (project.db) tables from `storage/tables.py`; `ProvidersService` (`get_model`, `get_provider_by_id`, `keys_for`); `ModelEntry`; `ResolvedModel` (`providers/bridge.py`); `AgentCatalog` (`get`, `all`); `DomainError`. Test fixtures `databases`, `keystore`, `paths` and the fake OpenAI-compatible server from `tests/conftest.py`, as `tests/providers/` uses them.
- Produces: I2 exactly as the overview states (`AgentLimitsUpdate`, `AgentSettingsView`, `AgentSettingsService` with `get`, `list_settings`, `set_default_model`, `set_project_model`, `set_project_limits`, `resolve`).

**Implementation notes:**
- Storage:
  - App default: one `agent_model_defaults` row per agent (`agent`, `model_address`). Setting `None` deletes the row.
  - Project overrides: `project_settings` rows with the keys `agent.<name>.model` (the address) and `agent.<name>.max_steps` (JSON: a number, or `null` for no limit). No row means no override. `set_project_model(..., None)` deletes the model row. A limits update always writes the row (`null` is a real override: "no limit").
- Callers pass a project id that exists (the API's `ProjectDep` and the runtime check it). The service never creates a project database for an unknown id on its own: it reads and writes only through `databases.project(project_id)` after the caller's check, and this is stated in the class docstring.
- Unknown agent (not in the catalog) → 404 `agent_not_found`, "There is no agent named <name>." in every method.
- A model is usable when it exists, `tool_calling` and `streaming` are verified (`availability == "verified"` and both traits true), and `context_window` is known. `set_default_model` and `set_project_model` refuse an address that is not usable: unknown → the providers service's 404 `model_not_found`; known but not usable → 409 `model_not_usable` with the reason (see the messages below). `None` is always accepted (it clears).
- `get` builds the view: `default_model`, `project_model` (None when `project_id` is None), `effective_model` (`project_model` or else `default_model`), `max_steps` (the project override if a row exists, else the definition's `limits.max_steps`), and `model_problem`: None when `effective_model` is usable, else the message `resolve` would raise. `list_settings` returns `get` for every agent in the catalog, sorted by name.
- `resolve(project_id, agent)` returns `(ResolvedModel, max_steps)`, or raises 409 with these codes and messages (the UI shows them as written):
  - no effective model → `model_unassigned`: "Choose a model for <display name> in AI providers, or in this project's settings."
  - the address no longer exists (a deleted model or provider: `agent_model_defaults` has no foreign key, and project overrides live in another database) → `model_missing`: "The model <address> no longer exists. Choose another model for <display name>."
  - not verified for tool calls or streaming → `model_not_usable`: "The model <address> has not passed verification (tool calls and streaming). Verify it in AI providers or choose another model."
  - no context window → `model_not_usable`: "The context window of <address> is unknown. Enter it in AI providers."
  - its provider's status is `needs_attention` → `provider_needs_attention`: "<provider> needs attention: <status_detail>. Fix it in AI providers."
  - a hosted provider with no keys → `provider_has_no_key`: "<provider> has no key. Add one in AI providers."
  The `ResolvedModel` carries the address, provider id and name, base URL, wire model id, context window and `keys_for(provider_id)` in order (rotation uses that order).
- All database calls are async (SQLAlchemy Core on the existing engines); no blocking calls on the event loop.

**Tests (behaviors to verify):** use real databases in a temporary data folder and a real `ProvidersService` against the fake OpenAI-compatible server, with models added and verified through the service (no hand-written rows), plus a test catalog holding the Orchestrator and a second definition from `tmp_path` (`AgentCatalog.load(..., extra_dirs=[...])`).
- App default: set, read back through `get` and `list_settings`, clear with `None`; setting an unknown address → 404 `model_not_found`; an untested model → 409 `model_not_usable`; a verified model without a context window → 409 naming the context window.
- Project override: overrides the default in `effective_model` for that project only (a second project still sees the default); clearing it falls back to the default; it needs no app default.
- Limits: the definition's value by default; a project override of 5 wins; `None` means no limit (`max_steps is None`) and is kept as an override, not mistaken for "no row"; `AgentLimitsUpdate(max_steps=0)` fails validation.
- `resolve`: returns the model with its keys in position order and `max_steps`; raises each of the six problems above with its code and message (delete the model; delete the provider; mark the provider `needs_attention` with `mark_needs_attention`; remove the hosted provider's last key; a self-hosted provider without a key resolves fine).
- `model_problem` in `get` matches the message `resolve` raises, and is None for a usable model.
- Unknown agent → 404 `agent_not_found` from `get`, the setters and `resolve`.
- No key appears in `AgentSettingsView` or in any error message (search the dumped views and messages for the test key).

**Checks:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`.

---

## Not in this plan

- The routes that expose these settings (api-resources) and the pages that edit them (frontend-providers, frontend-workspace).
- Using `resolve` at the start of a turn, and key rotation (agent-runtime).
