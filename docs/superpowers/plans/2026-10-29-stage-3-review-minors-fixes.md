# Stage 3 — Review Minors Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** Fix the review findings no plan owns: bug #380 (items 2, 4, 5 and 6; items 1 and 3 belong to the approvals-fixes plan #390) and bug #311.

**Branch:** `plan/stage-3-review-minors-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every public interface keeps its signature.

**Read first:** bugs #380 and #311; `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Approvals and contracts minors (#380 items 2, 4, 5, 6) | merged code |
| 2 | Task resume event and session snapshot order (#311) | merged code |

---

### Task 1: Approvals and contracts minors (#380 items 2, 4, 5, 6)

**Files:** `frontend/eslint.config.js`; `backend/src/mlops_agents/storage/sqlite.py` (or the module that opens SQLite connections: the one place the floor is checked); `backend/src/mlops_agents/api/services.py` (one comment); the four test files split by concern: `backend/tests/agents/runtime/test_contract.py`, `backend/tests/agents/runtime/test_context.py`, `backend/tests/agents/test_approval_gate.py`, `frontend/src/pages/inbox/InboxPage.test.tsx`, into new sibling test files.

**Implementation notes:**
- **Item 2.** Add `CARD_FOR_KIND` and `CARD_FOR_PROPOSAL` to `allowExportNames` in `frontend/eslint.config.js`, next to the existing `route` entry. `npm run lint` shows no warning.
- **Item 4.** The approval sweeps use `UPDATE ... RETURNING`, which needs SQLite 3.35 or later. Check `sqlite3.sqlite_version_info` once, where the app opens its databases, and refuse to start with a message naming the version found and the version needed. The minimum is one constant.
- **Item 5.** The comment above `await wakeups.start()` in `api/services.py` names the call: observers are registered on the store (`approval_store.observers.add(...)`) above that line.
- **Item 6.** Split each of the four test files by concern into files under about 400 lines; move tests, do not change them; shared setup goes in one helper, not copies.
- The rate-limits plan (in progress) also edits `api/services.py`: touch only that comment line there.

**Tests (stop condition):** lint has no warnings; a test with `sqlite3.sqlite_version_info` patched to (3, 34, 0) shows the clear startup error, and the real version passes; the test count of the four split files equals the count before (stated in the PR); full backend suite, ruff, format, pyright; frontend lint, format check, tests, build; CI green.

---

### Task 2: Task resume event and session snapshot order (#311)

**Files:** `backend/src/mlops_agents/agents/tasks/service.py` (`resume`), `backend/src/mlops_agents/agents/tools/tasks.py`, `frontend/src/pages/agents/session/useSession.ts` (`refreshTask`), `frontend/src/pages/agents/AgentsPage.states.test.tsx`, `frontend/src/pages/project-settings/ProjectSettingsPage.states.test.tsx`, and the tests of the files changed.

**Implementation notes:**
- **Resume records an event.** Resuming a task records a `task.*` event through the same path `start` uses (reuse the existing event type that means "this session's task changed", if one exists; a new event type needs a Decision issue), so the session snapshot refreshes and the task bar appears at once, not at `turn.ended`.
- **Only the latest snapshot is applied.** In `refreshTask`, a response that is older than one already applied (a request counter or an abort of the previous request) is dropped, so a stale response cannot bring back a task that ended.
- **Tests that fail on the old code.** `AgentsPage.states.test.tsx` and the no-request half of `ProjectSettingsPage.states.test.tsx` pass on the old code; rewrite them so each fails on the old code (check it by reverting). Add: Escape closes the discard dialog when nothing is pending.

**Tests (stop condition):** resuming a task records the event and the snapshot shows the task; two snapshot responses arriving out of order leave the newer one applied; the rewritten tests fail on the old code (stated in the PR); Escape closes the discard dialog; full backend suite, ruff, format, pyright; frontend lint, format check, tests, build; CI green.
