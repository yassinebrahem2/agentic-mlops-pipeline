# Stage 2 — Frontend Agents Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix bug #289, found in the reviews of #287.

**Branch:** `plan/stage-2-agents-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug #289; the frontend-agents plan (`2026-10-24-stage-2-frontend-agents.md`), tasks 3 and 4.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Frontend agents review fixes (#289) | merged code |

---

### Task 1: Frontend agents review fixes (#289)

**Files:** `frontend/src/pages/agents/ApprovalCard.tsx`, `ApprovalCard.test.tsx`, `AgentsPage.tsx`, `TaskBar.tsx` (the discard dialog) and their tests; `frontend/src/pages/project-home/ProjectHome.tsx`; `frontend/src/pages/agents/session/useSession.ts` if the snapshot needs the refresh; the approval step in `frontend/e2e/flow.spec.ts`; `backend/tests/fakes/fake_openai.py` (the `fake-write` model).

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- **Click-twice test.** Send both clicks inside one `act()` (two native `button.click()` calls), so the second click reaches the handler before React re-renders. Check that removing `if (submittingRef.current) return;` makes the test fail, then restore it.
- **Task bar source.** `AgentsPage.tsx` shows `session.state?.current_task ?? selectedAgentStatus?.task`. The session snapshot is the one source: show `session.state?.current_task` only, and make sure the snapshot refreshes on `task.*` events (add that if it does not), so the bar still appears as soon as a task starts.
- **No empty project ID.** `AgentsPage.tsx:33` (`useSession(projectId ?? "", ...)`), `ApprovalCard.tsx:120`, `TaskBar.tsx:27` and `ProjectHome.tsx:9` fall back to `""`. A hook or route that needs a project is never called with an empty ID: render nothing, or keep the query disabled, until the project ID is known.
- **E2E final reply.** The approval step asserts the final reply text ("I have started the task and written NOTES.md.").
- **`fake-write`.** Its "pull request" check reads only the last user message, never tool results.
- **Discard dialog.** While the discard request is pending, Escape and an outside click leave the dialog open.

**Tests (stop condition):**
- The click-twice test above, failing without the guard.
- The task bar hides a task the session snapshot says has ended, even while the status list still shows it.
- The dialog stays open on Escape while the request is pending.
- None of the four components calls a project route before the project ID is known.
- `tests/fakes` and the e2e pass.
- Frontend lint, format, tests, build and e2e; backend ruff, format, pyright; CI green on every job.
