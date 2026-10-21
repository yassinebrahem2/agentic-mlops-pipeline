# Stage 1 — Frontend Providers Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The app-level pages: AI providers (providers, keys, keys detected in the environment, models with verification, and the default model of each agent), Integrations (GitHub), and app Settings (data folder, run mode, backup).

**Branch:** `plan/stage-1-frontend-providers`. **Consumes:** I5 (frontend-foundation tasks 1 and 2), I6 (api-resources). **Produces:** the pages at `/providers`, `/integrations` and `/settings` (I5's page paths).

**Read first:** the stage overview (global constraints, I2, I5, I6, the stage 1 end-to-end flow step 2), spec sections 3, 4 and 6, and the merged `ProvidersService` and `GitHubAuth` for the error codes below.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | AI providers: providers, keys, detected keys, models | I5 on `main`; api-resources task 3 (#52) |
| 2 | Default model per agent | task 1; api-resources task 4 (#53) |
| 3 | Integrations and app Settings | I5 on `main`; api-resources tasks 1 and 2 (#50, #51) |

Task 3 does not depend on tasks 1 and 2; do it whenever its routes are on `main`.

The rules for every page in the frontend-workspace plan ("Rules for every page") apply here word for word: one folder per page with a `route.tsx` exporting only `route`, TanStack Query with `unwrap` and resource-first keys (`["providers"]`, `["models"]`, `["remote-models", name]`, `["env-keys"]`, `["agents"]`, `["github"]`, `["app"]`), `errorText` next to the failed action, real error codes only, labels on every control, tests through `renderPage` with a stubbed `fetch`.

---

### Task 1: AI providers: providers, keys, detected keys, models

**Files:**
- Create: `frontend/src/pages/providers/route.tsx`, `ProvidersPage.tsx`, `ProviderCard.tsx`, `AddProviderDialog.tsx`, `DetectedKeys.tsx`, `ModelsTable.tsx`, `AddModelDialog.tsx`
- Test: `ProvidersPage.test.tsx`, `ModelsTable.test.tsx`

**Interfaces:**
- Consumes: I6 `GET/POST /providers`, `DELETE /providers/{name}`, `POST /providers/{name}/keys`, `DELETE /providers/{name}/keys/{position}`, `GET /providers/{name}/remote-models`, `GET /env-keys`, `POST /env-keys/{env_var}/import`, `GET/POST /models`, `DELETE /models?address=`, `POST /models/verify`, `PUT /models/context-window`; the codes `provider_exists`, `key_exists`, `key_rejected`, `key_empty`, `no_keys`, `model_exists`, `verification_failed`, `invalid_context_window`, `provider_unreachable`, `provider_error`.
- Produces: `route` at `/providers` with `nav: { area: "app", label: "AI providers", order: 10 }`.

**Implementation notes:**
- Providers: one card each with name, kind ("Hosted" / "Self-hosted"), base URL, status (`needs_attention` shows its detail as a warning with "Fix the key below"), and its keys as masked hints, each with "Remove". "Add key" opens a password-type input; the key is sent once and never shown again (the response carries the hint only); a rejected key shows the server's message (`key_rejected`). "Remove provider" asks first. After adding or removing a key, the card re-renders from the returned `Provider` (positions shift after a removal; never reuse positions cached earlier).
- "Add provider": name (lowercase letters, digits and dashes), kind, base URL (for self-hosted the hint "for example http://localhost:11434/v1 for Ollama"), and for hosted, an optional first key. A 422 shows each field's message under that field (`fieldErrors`, I5). With a first key it is two requests in order: `POST /providers`, then `POST /providers/{name}/keys`. When the key is rejected, the provider stays (without a key), the dialog shows `key_rejected`'s message, and closing it leaves the provider's card with "Add key".
- Detected keys: `GET /env-keys` lists environment variables the server can see, masked, with "Import" each (or "Import key" when the provider already exists). Shown only when the list is not empty.
- Models: a table with address, tool calling, streaming, context window, availability (with its detail when `needs_attention`), last verified, and actions "Verify" and "Remove".
  - "Verify" makes a real model call that can take seconds: the button shows progress and is disabled until the answer; the row updates from the response; `verification_failed` shows its message on the row.
  - A row whose context window is unknown has an inline number input "Context window" and "Save" (`PUT /models/context-window`). The row says the model cannot be assigned until it is set (spec section 3).
- "Add model": choose the provider, then a model from its remote list (`GET /providers/{name}/remote-models`, with a filter box) or type a model id; an optional context window. A provider whose listing fails still allows typing (show the error above the input).
- Address values contain `/` and `:`; they are only ever sent in bodies or query strings (I6).

**Tests (behaviors to verify):** the providers and their masked keys render; adding a self-hosted provider without a key sends `{name, kind: "self_hosted", base_url}`; a 422 on the base URL shows the validator's own sentence under the base URL field; a hosted provider with a first key that the server rejects stays listed without a key and the dialog shows the rejection; adding a key sends it once and the input clears, and the key text appears nowhere in the page afterwards; a `needs_attention` provider shows its detail; detected keys import; the models table shows traits and availability; "Verify" disables itself while the request is pending and shows the result; `verification_failed` shows on the row; an unknown context window offers the input and sends `{address, value}`; "Add model" lists remote models, filters them, and sends the chosen id; removing a model whose address contains `/` sends it in the query string.

**Checks (from `frontend/`):** `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---

### Task 2: Default model per agent

**Files:**
- Create: `frontend/src/pages/providers/AgentDefaults.tsx`
- Modify: `frontend/src/pages/providers/ProvidersPage.tsx` (the section)
- Test: `AgentDefaults.test.tsx`

**Interfaces:**
- Consumes: I6 `GET /agents`, `PUT /agents/{agent}/model`, `GET /models`, `GET /providers`; `isUsableModel` and `hostedModelWarning` from `src/lib/models.ts` (frontend-foundation task 2); the code `model_not_usable`.

**Implementation notes:**
- A section "Default model per agent": one row per agent (display name, description), a "Model" select with "None" and the usable models (`isUsableModel`). The current `default_model` is always an option, labelled "<address> (not usable)" when it fails the rule or is missing from `GET /models`, so the select never shows a value that is not the real one; and the agent's `settings.model_problem` as a warning when set (for example "Choose a model for Orchestrator ...").
- When the chosen model belongs to a `hosted` provider (by `provider_name` in `GET /providers`), show `hostedModelWarning(provider)` under the row (spec section 5).
- Choosing sends `PUT /agents/{agent}/model` with the address, or `null` for None, and refreshes `["agents"]`.

**Tests (behaviors to verify):** the select offers only usable models plus None, and a current default that is no longer usable appears as "<address> (not usable)" and stays selected; choosing sends the address and the row's problem disappears after the refetch; None sends `null`; the hosted warning is shown for a hosted model and absent for a self-hosted one; `model_problem` is shown when the agent has no model.

**Checks:** as task 1.

---

### Task 3: Integrations and app Settings

**Files:**
- Create: `frontend/src/pages/integrations/route.tsx`, `IntegrationsPage.tsx`; `frontend/src/pages/app-settings/route.tsx`, `AppSettingsPage.tsx`
- Test: `IntegrationsPage.test.tsx`, `AppSettingsPage.test.tsx`

**Interfaces:**
- Consumes: I6 `GET /github`, `POST /github/gh`, `POST /github/token`, `DELETE /github`, `GET /app`, `POST /app/backup`; the codes `gh_not_installed`, `gh_not_logged_in`, `gh_timeout`, `github_token_rejected`, `github_unreachable`, `token_empty`.
- Produces: `route` at `/integrations` with `nav: { area: "app", label: "Integrations", order: 20 }`; `route` at `/settings` with `nav: { area: "app", label: "App settings", order: 30 }`.

**Implementation notes:**
- Integrations, GitHub card:
  - connected: "Connected as <login>" and how ("through the GitHub CLI" or "with a pasted token"), and "Disconnect" (asks first);
  - not connected: "Use my GitHub CLI login" when `gh_available` (the server's message when `gh` is not logged in), and "Paste a token": a password-type input with a short note naming the permissions a fine-grained token needs to read and clone the user's repositories (contents: read, metadata: read), and "Connect".
  - The token is never shown again after it is sent.
- App Settings: app name and version, run mode ("Local" / "Server"), data folder (with a "Copy" button), and "Back up now" (`POST /app/backup`), which shows the folder the backup was written to.

**Tests (behaviors to verify):** the three GitHub states (none with and without `gh`, connected through `gh`, connected with a token); "Use my GitHub CLI login" shows `gh_not_logged_in`'s message when the server returns it; a pasted token is sent once, the input clears and the token text appears nowhere afterwards; a rejected token shows the server's message; Disconnect asks, then sends `DELETE`; app Settings shows the mode and data folder, and "Back up now" shows the returned path.

**Checks:** as task 1.

---

## Not in this plan

- Project pages (frontend-workspace) and the chat page (frontend-chat).
