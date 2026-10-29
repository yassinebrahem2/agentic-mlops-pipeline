# Stage 3 — Rate-Limits Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Three tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** Fix bug #427, the findings of the review of #424 (the rate-limits plan).

**Branch:** `plan/stage-3-rate-limits-fixes`. **Consumes:** merged code; task 3 also consumes the approvals-fixes plan (#390) merged. **Produces:** nothing other plans use; every public interface keeps its signature.

**Read first:** bug #427; the rate-limits plan (`2026-10-28-stage-3-rate-limits.md`) and Decision #384; `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Backend fixes (#427 items 1, 2 backend half, 4, 6, 7 `providers/service.py`) | merged code |
| 2 | Frontend fixes (#427 items 2, 8) | task 1 |
| 3 | After approvals-fixes (#427 items 3, 5, 7 `sessions.py` and `test_stage3_schema.py`) | #390 merged |

Task 3 waits because the approvals-fixes plan (#390) edits `graph_run.py`, `sessions.py` and `test_stage3_schema.py`. Start it once #390 is merged and bring `main` in first; until then, tasks 1 and 2 are done and pushed and the plan PR waits. The onboarding plan (#360) also edits `context.py` and `sessions.py`; it starts only after this plan's PR has merged.

---

### Task 1: Backend fixes

**Files:** `backend/src/mlops_agents/providers/failover.py`; `agents/runtime/context.py` and the tests that call `build_messages`; `agents/runtime/model_caller.py` (where a resumed turn resolves its model); `providers/service.py`, split into sibling modules in `providers/`; new `shared/fixtures/describe-keys.json`; their tests.

**Implementation notes:**
- **Item 1: a keyless provider retries a transient error.** Both `if not provider.keys: result = await attempt(None)` branches in `KeyFailover` (`call` and its streaming twin) make one attempt. A keyless provider retries the same call after each of `TRANSIENT_BACKOFFS` on a `TRANSIENT` or `UNREACHABLE` error (`_SAME_KEY_RETRY`), as a keyed provider retries its key, then raises the last error. Write the retry once and use it for the keyed and keyless paths. A keyless provider is still never marked (Decision #384).
- **Item 2, backend half: one fixture for the keys' wording.** New `shared/fixtures/describe-keys.json`, like `shared/fixtures/session-fold.json`: a list of cases, each with the `KeyState` list, the exact `describe_keys` text, and the rows the stall card shows for it. Cover every `KeyStatus`, a key with and without `since`, and one, two and three keys. A backend test pins `describe_keys(states) == text` for every case. Task 2 makes the frontend parse against the same file.
- **Item 4: no test-only code.** Delete `build_messages` from `context.py`; its tests build the messages with `system_message` plus `history_messages`, the path the runtime uses.
- **Item 6: a provider deleted while stalled.** After Try again on a stall whose provider was deleted, the turn continues with the agent's model. When that model's provider is gone, the turn fails with `provider_not_found`'s own message, not the generic failure text.
- **Item 7, `providers/service.py` (449 lines):** split by concern into files under about 400 lines; move code, change no behaviour; public names keep their import path (the module re-exports what callers import, or the callers' imports change in the same commit).

**Tests (stop condition):** a keyless provider answering 503 then 200 succeeds after one backoff (fake clock or patched sleep, no real wait), and an `UNREACHABLE` error is retried the same way; a keyless provider failing every attempt raises the last error after `len(TRANSIENT_BACKOFFS)` retries and is not marked; the keyed path's existing retry tests pass unchanged; the fixture test passes for every case; no `build_messages` remains (`git grep` in the PR); a deleted provider after Try again fails the turn with the text of the `provider_not_found` `DomainError` the lookup raised (asserted against that error's message, not a copied string); full backend suite, ruff, format, pyright; CI green.

---

### Task 2: Frontend fixes

**Files:** `frontend/src/pages/agents/StallCard.tsx`, `StallCard.test.tsx`; `frontend/src/pages/providers/ProviderCard.tsx` and its test.

**Implementation notes:**
- **Item 2, frontend half.** `StallCard.test.tsx` imports `shared/fixtures/describe-keys.json` (as `applyEvent.test.ts` imports `session-fold.json`) and checks that the card's split of each case's text gives that case's rows. The split stays in `StallCard.tsx`; the fixture is its specification.
- **Item 8: one source for the cooldown range.** Delete `COOLDOWN_MIN` and `COOLDOWN_MAX` from `ProviderCard.tsx`. The backend's `Field(ge=1, le=1440)` in `api/routers/providers.py` is the only source: the field sends any whole number and shows the server's 422 message (through the app's existing API error text) when it is out of range. A non-number is still refused before sending.
- **Item 8: the field follows the provider.** When `provider.cooldown_minutes` changes (another tab, a refetch) and the field has no unsaved edit, the field shows the new value.

**Tests (stop condition):** the fixture test passes for every case; a value of 0 and of 1441 is sent and the server's message is shown; a refetched provider with a new cooldown updates an unedited field and leaves an edited one alone; frontend lint, format check, tests, build; CI green.

---

### Task 3: After approvals-fixes

Start only once #390 is merged; bring `main` in first.

**Files:** `backend/src/mlops_agents/agents/runtime/graph_run.py`; `agents/runtime/sessions.py`, split into sibling modules; `backend/tests/storage/test_stage3_schema.py`, split into sibling test files; their tests.

**Implementation notes:**
- **Item 3: a failed stall read is not hidden.** In `graph_run.py`, when reading the interrupted graph's state fails, the turn fails with the error logged at warning (with the turn ID), so a waiting turn never sits without its card. Logging at debug and going on is what the bug reports.
- **Item 5: Stop between the stall and `waiting`.** Check that #390's sweep at every turn end settles a stall recorded just before a Stop. Write the test: a Stop arriving after `create_stall` and before the turn is `waiting` leaves no pending stall, and a later Try again on it is refused (the turn is stopped), never "Resumed". If the test fails on `main` after #390, fix it in the turn-end sweep's own module and say so in the PR.
- **Item 7:** split `sessions.py` (403 lines, if still over about 400 after #390) and `test_stage3_schema.py` (546 lines) by concern; move code and tests, do not change them; the split test files keep the same test count (state the before and after counts in the PR).

**Tests (stop condition):** a patched `aget_state` failure fails the turn and logs a warning; the Stop-race test; the test counts; full backend suite, ruff, format, pyright; CI green.
