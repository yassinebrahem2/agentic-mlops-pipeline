# Stage 1 — Frontend Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix two frontend defects found in review: the project queries repeated in each page (#117), and frontend-chat's reconnect, copied test setup and repeated event names (#125). Split out of the fixes plan so a second owner can take them.

**Branch:** `plan/stage-1-frontend-fixes`. **Consumes:** merged code only. **Produces:** nothing other plans use.

**Read first:** bug issues #117 and #125, and `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One source for the project queries (#117) | merged code |
| 2 | Agents page: reconnect retries, shared test setup, one list of event names (#125) | merged code |

---

### Task 1: One source for the project queries (#117)

**Files:** create `frontend/src/lib/useProject.ts`, `frontend/src/lib/useProjectAgents.ts` and their tests; modify `frontend/src/pages/agents/AgentsPage.tsx`, `frontend/src/pages/project-settings/ProjectSettingsPage.tsx`, `AgentsSection.tsx`, `RemoveProjectDialog.tsx`, `RepositorySection.tsx`.

**Interfaces:** Consumes merged code. Produces `useProject(projectId)`, `useProjectAgents(projectId)`, and the key builders `projectKey(projectId)` (`["projects", projectId]`) and `projectAgentsKey(projectId)` (`["projects", projectId, "agents"]`), in the style of `useModels`.

**Implementation notes:** every page reads these through the hooks, and every `setQueryData`, `invalidateQueries` and `removeQueries` on them uses the key builders, so no file writes these keys or paths inline any more. Behavior is unchanged. (`useOpenProject` is a mutation that opens a project and only invalidates `["projects"]`; it is unrelated and stays as it is.)

**Tests (stop condition), from `frontend/`:** a test per hook (key, path, data); `git grep -n '"projects", project' -- frontend/src ':!frontend/src/lib/*'` finds nothing; `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: Agents page: reconnect retries, shared test setup, one list of event names (#125)

**Files:** `frontend/src/pages/agents/session/useSession.ts`, `events.ts`, `useSession.test.tsx`, `useSessionStream.test.tsx`, `frontend/src/pages/agents/AgentsPage.test.tsx`, `AgentsPageStatus.test.tsx`, and two new test-only helpers, `frontend/src/pages/agents/session/testing.tsx` and `frontend/src/pages/agents/testing.tsx`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- A reconnect whose snapshot reload fails with anything but a refusal (401, 404) schedules the next retry with the same backoff as a stream error; only a refusal closes the stream for good (`useSession.ts:145-149` today sets "closed" and stops).
- The event names have one source: `events.ts` exports them, and `useSession.ts` uses that export instead of its own `eventNames` list.
- The fake stream, `initialSession`, wrapper and spies copied between `useSession.test.tsx` and `useSessionStream.test.tsx`, and between `AgentsPage.test.tsx` and `AgentsPageStatus.test.tsx`, move into one shared test helper per pair.

**Tests (stop condition), from `frontend/`:** a test where the reload fails with a 500, and one where it fails with a network error, during a reconnect, each, then succeeds on the next scheduled retry (fake timers), and one where it fails with a 404 and the stream stays closed; `npm test` 3 times (nothing else heavy running), lint, format check, build; CI green.
