# Stage 3 — Review Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Three tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** Fix the review findings that are still real under the bar of 2026-10-29: a test that proves nothing, a gap in the prompt-safety escape, and a secret check that is missing. Every other item of these bugs is recorded in `docs/known-debt.md`.

**Branch:** `plan/stage-3-review-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every public interface keeps its signature.

**Read first:** bugs #437, #434 and #435 (their "Update 2026-10-29" sections); `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The stale-snapshot test proves the guard (#437 item 1) | merged code |
| 2 | The data-summary escape and the hostile-name test (#434 items 2, 3) | merged code |
| 3 | A tracker credential never comes back in a reply (#435 item 2) | merged code |

---

### Task 1: The stale-snapshot test proves the guard (#437 item 1)

**Files:** `frontend/src/pages/agents/session/useSession.test.tsx`.

**Notes:** "drops stale session snapshot responses arriving out of order" passes on the hook before #436. Its last assertion runs right after a synchronous `act()`, before the late response's `.then` has run. Await the late response, then assert.

**Tests (stop condition):** the test fails on `useSession.ts` from before #436 (`git show 2f5c92c^1:frontend/src/pages/agents/session/useSession.ts`) and passes on `main`; state both runs in the PR; frontend lint, format check, tests; CI green.

---

### Task 2: The data-summary escape and the hostile-name test (#434 items 2, 3)

**Files:** `backend/src/mlops_agents/agents/hooks/registry.py` (`_DATA_TAG`, the #386 escape) and its tests; `backend/tests/data/test_duck_lockdown.py` (`test_hostile_file_and_column_names_change_nothing`).

**Notes:**
- **The escape misses `< data`** (a space and no slash): `_DATA_TAG` is `<(?=(?:\s*/\s*)?data(?![\w-]))`. Use `<(?=\s*/?\s*data(?![\w-]))` (keep `re.IGNORECASE`), so text read from the team's files can never open or close the wrapper tag.
- **The hostile file-name test proves nothing:** its payload writes to a path outside the folder, so the folder lock refuses it even when quoting is broken. `chdir` into the connected folder so a quoting failure would really write there, and add hostile VARCHAR column headers so the top-values query sees them.

**Tests (stop condition):** `< data`, `</ data>` and `<  /data` in a cell are escaped; the hostile-name test fails when the one quoting function is replaced by plain string formatting (check it by reverting locally; state it in the PR); full backend suite, ruff, format, pyright; CI green.

---

### Task 3: A tracker credential never comes back in a reply (#435 item 2)

**Files:** `backend/tests/tracking/test_sources.py`.

**Notes:** the test sends a source URL with `user:pass@` and checks only the 422. Also assert that neither the user nor the password appears anywhere in the response body.

**Tests (stop condition):** the assertion fails if the route echoes the URL back (check it by making it echo locally; state it in the PR); backend suite, ruff, pyright; CI green.
