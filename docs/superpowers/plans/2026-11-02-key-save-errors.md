# Providers: Saving a Key Explains Every Refusal Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #698 and a finding from the lead's walkthrough (2026-11-02). (1) On Windows, saving a value longer than 1280 characters returns a 500: Windows Credential Manager refuses it with `pywintypes.error (1783, 'CredWrite', ...)`, which is not a `keyring.errors.KeyringError`, so `KeyringStore.set` lets it escape. (2) When a provider refuses the key check with 401 or 403, `ProvidersService._get` says "<provider> rejected this key." and drops the provider's own message, so a refusal that is not about the key (AgentRouter's "unauthorized client detected, contact support") sends the user after a key problem that does not exist.

**Branch:** `plan/key-save-errors`. **Milestone:** Stage 1: Foundation and first agent. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #698.

**Read first:** bug #698; `backend/src/mlops_agents/credentials/keyring_store.py` (`set`, `_HELP`), `credentials/scrub.py` (`scrub_values`), `providers/service.py` (`add_key`, `_get`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A refused key says why | merged code |

---

### Task 1: A refused key says why

**Files:** `backend/src/mlops_agents/credentials/keyring_store.py` (and the credentials module's error types); `backend/src/mlops_agents/api/errors.py` (one mapping); `backend/src/mlops_agents/providers/service.py` (`_get`'s 401/403 branch); tests under `backend/tests/credentials/` and `backend/tests/providers/`.

**Notes:**
- `KeyringStore.set` turns any refusal of the OS keyring's write into `KeyStoreError` (the same message shape as today's); a value over the limit is refused before the write with its own error type, a subclass of `KeyStoreError`, which the API maps to 422 with its message (every route that stores a secret, not only provider keys; a refused write of the OS keyring stays 503 `keystore_unavailable`, a server-side problem). The message says the value is too long to be a key and gives the limit, one constant beside `KeyringStore` under one name, and does not claim every keyring has that limit (it is Windows Credential Manager's). The value itself never appears in a message or a log.
- On 401 or 403 the message keeps "<provider> refused the key" and adds the provider's own error text (`error.message` or `message` of a JSON body, else the start of the text), passed through `scrub_values` with the key and capped like other provider errors.

**Tests (stop condition):** first, one that fails on `main`: a keyring backend whose write raises a non-`KeyringError` exception gives `KeyStoreError` (and the route a 4xx with the message, never a 500); a 1281-character value is refused before the write; a 401 with `{"error":{"message":"unauthorized client detected"}}` gives a message containing that text and not the key; a key echoed in the body is scrubbed; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
