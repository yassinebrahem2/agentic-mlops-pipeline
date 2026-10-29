# Stage 3 — Rate-Limits Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** Fix the behaviour findings of bug #427 (the review of #424): items 1, 3, 5 and 6. Items 2, 4, 7 and 8 (a shared wording fixture, a test-only function, file splits, a copied cooldown range) are recorded in `docs/known-debt.md` under the lowered bar of 2026-10-29 and are not part of this plan.

**Branch:** `plan/stage-3-rate-limits-fixes`. **Consumes:** merged code; task 2 also consumes the approvals-fixes plan (#390) merged. **Produces:** nothing other plans use; every public interface keeps its signature.

**Read first:** bug #427; the rate-limits plan (`2026-10-28-stage-3-rate-limits.md`) and Decision #384; `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Keyless retry and a deleted provider (#427 items 1, 6) | merged code |
| 2 | After approvals-fixes (#427 items 3, 5) | #390 merged |

Task 2 waits because the approvals-fixes plan (#390) edits `graph_run.py`. Start it once #390 is merged and bring `main` in first; until then, task 1 is done and pushed and the plan PR waits.

---

### Task 1: Keyless retry and a deleted provider

**Files:** `backend/src/mlops_agents/providers/failover.py`; `agents/runtime/executor.py` (`run` and `resume`); their tests.

**Implementation notes:**
- **Item 1: a keyless provider retries a transient error.** Both `if not provider.keys: result = await attempt(None)` branches in `KeyFailover` (`call` and its streaming twin) make one attempt. A keyless provider retries the same call after each of `TRANSIENT_BACKOFFS` on a `TRANSIENT` or `UNREACHABLE` error (`_SAME_KEY_RETRY`), as a keyed provider retries its key, then raises the last error. Write the retry once and use it for the keyed and keyless paths. A keyless provider is still never marked (Decision #384).
- **Item 6: a provider deleted while stalled** (Decision #443). Deleting a provider deletes its models, so after Try again the builder refuses the resumed turn (`TurnRefused`, from `model_missing` in `AgentSettingsService.resolve`). `resume` handles a `TurnRefused` exactly as `run` does, through one function both call: an end of `done` ends the turn done; any other end posts the system message and fails the turn with the refusal's own text, never "failed unexpectedly". Where resume must differ (its turn already has messages), the shared function takes that as a parameter; say so in the PR. This covers every refusal on resume (missing project, no model, unusable model), not only a deleted provider.

**Tests (stop condition):** a keyless provider answering 503 then 200 succeeds after one backoff (fake clock or patched sleep, no real wait), and an `UNREACHABLE` error is retried the same way; a keyless provider failing every attempt raises the last error and is not marked; the keyed path's existing retry tests pass unchanged; a deleted provider after Try again fails the turn with the refusal's own text (asserted against the raised error's message, not a copied string); a refusal with end `done` on resume ends the turn done, as on `run`; full backend suite, ruff, format, pyright; CI green.

---

### Task 2: After approvals-fixes

Start only once #390 is merged; bring `main` in first.

**Files:** `backend/src/mlops_agents/agents/runtime/graph_run.py`; the turn-end sweep's module only if item 5's test fails; their tests.

**Implementation notes:**
- **Item 3: a failed stall read is not hidden.** In `graph_run.py`, when reading the interrupted graph's state fails, the turn fails with the error logged at warning (with the turn ID), so a waiting turn never sits without its card. Logging at debug and going on is what the bug reports.
- **Item 5: Stop between the stall and `waiting`.** Check that #390's sweep at every turn end settles a stall recorded just before a Stop. Write the test: a Stop arriving after `create_stall` and before the turn is `waiting` leaves no pending stall, and a later Try again on it is refused (the turn is stopped), never "Resumed". If the test fails on `main` after #390, fix it in the turn-end sweep's own module and say so in the PR.

**Tests (stop condition):** a patched `aget_state` failure fails the turn and logs a warning; the Stop-race test; full backend suite, ruff, format, pyright; CI green.
