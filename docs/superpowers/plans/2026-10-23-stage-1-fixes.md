# Stage 1 — Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix small defects found in review: a timing assertion and inline lint suppressions in api-sessions' test code (#119), the board automation moving a closed task when a Decision closes (#121), the project queries repeated in each page (#117), and frontend-chat's reconnect, copied test setup and repeated event names (#125).

**Branch:** `plan/stage-1-fixes`. **Consumes:** merged code only. **Produces:** nothing other plans use.

**Read first:** bug issues #119, #121, #117 and #125, and `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Session live test: no timing assertion, no inline suppressions (#119) | merged code |
| 2 | Board: a Decision never moves a closed task (#121) | merged code |
| 3 | One source for the project queries (#117) | merged code |
| 4 | Agents page: reconnect retries, shared test setup, one list of event names (#125) | merged code |

---

### Task 1: Session live test: no timing assertion, no inline suppressions (#119)

**Files:** `backend/tests/api/test_sessions_live.py`, `backend/tests/fakes/fake_openai.py`, `backend/pyproject.toml`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- In the clean-shutdown test, drop `started = time.monotonic()` and the `assert time.monotonic() - started < 5.0`. The `asyncio.timeout(5.0)` around `live.stop()` already fails the test if shutdown hangs. Drop the `time` import if nothing else uses it.
- In `fake_openai.py`, the hold gate's wait loop carries an inline `pyright: ignore[reportPrivateUsage]` and `noqa: ASYNC110`. Test-only exceptions go in the linter's per-file config (AGENTS.md). Add `"tests/fakes/fake_openai.py" = ["ASYNC110"]` under `[tool.ruff.lint.per-file-ignores]`, and under `[tool.pyright]` add `executionEnvironments = [{ root = "tests/fakes", extraPaths = ["src", "../scripts"], reportPrivateUsage = false }, { root = "." }]` (pyright 1.1.414 accepts it; tried). A one-line comment in `pyproject.toml` says why: the fake server polls the gate across threads. Remove both inline suppressions. The public gate stays `hold_slow()` / `release_slow()` only.

**Tests (stop condition), from `backend/`:** `uv run pytest tests/api/test_sessions_live.py` passes 5 times; full suite, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` pass with no inline suppression left in either file; CI green on Linux and Windows.

---

### Task 2: Board: a Decision never moves a closed task (#121)

**Files:** `.github/workflows/board.yml`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- The Decision branch (`issues/opened|issues/closed` with the `decision` label) moves the "Blocked task" to Blocked on open and to In progress on close without checking the task's state. Move it only when the task is open, using the same check `move_closed` uses (`gh issue view <n> --json state`). Do not copy the check: factor it into one shell function that both `move_closed` and the Decision branch call.

**Tests (stop condition):** `actionlint .github/workflows/board.yml` (actionlint 1.7.7 release binary) reports no error, and CI is green. Issue events run the workflow from `main` only, so the behavior cannot be exercised on the plan branch: the PR says so, and the lead checks it on `main` after merging (a scratch Decision naming a closed task leaves that task in Done).

---

### Task 3: One source for the project queries (#117)

**Files:** create `frontend/src/lib/useProject.ts`, `frontend/src/lib/useProjectAgents.ts` and their tests; modify `frontend/src/pages/agents/AgentsPage.tsx`, `frontend/src/pages/project-settings/ProjectSettingsPage.tsx`, `AgentsSection.tsx`, `RemoveProjectDialog.tsx`, `RepositorySection.tsx`.

**Interfaces:** Consumes merged code. Produces `useProject(projectId)`, `useProjectAgents(projectId)`, and the key builders `projectKey(projectId)` (`["projects", projectId]`) and `projectAgentsKey(projectId)` (`["projects", projectId, "agents"]`), in the style of `useModels`.

**Implementation notes:** every page reads these through the hooks, and every `setQueryData`, `invalidateQueries` and `removeQueries` on them uses the key builders, so no file writes these keys or paths inline any more. Behavior is unchanged.

**Tests (stop condition), from `frontend/`:** a test per hook (key, path, data); `git grep -n '"projects", project' -- frontend/src ':!frontend/src/lib/*'` finds nothing; `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 4: Agents page: reconnect retries, shared test setup, one list of event names (#125)

**Files:** `frontend/src/pages/agents/session/useSession.ts`, `events.ts`, `useSession.test.tsx`, `useSessionStream.test.tsx`, `frontend/src/pages/agents/AgentsPage.test.tsx`, `AgentsPageStatus.test.tsx`, and new shared test helpers next to them (for example `session/testing.ts` and `agents/testing.tsx`, test-only).

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- A reconnect whose snapshot reload fails with anything but a refusal (401, 404) schedules the next retry with the same backoff as a stream error; only a refusal closes the stream for good (`useSession.ts:145-149` today sets "closed" and stops).
- The event names have one source: `events.ts` exports them, and `useSession.ts` uses that export instead of its own `eventNames` list.
- The fake stream, `initialSession`, wrapper and spies copied between `useSession.test.tsx` and `useSessionStream.test.tsx`, and between `AgentsPage.test.tsx` and `AgentsPageStatus.test.tsx`, move into one shared test helper per pair.

**Tests (stop condition), from `frontend/`:** a test where the reload fails with a 500 during a reconnect, then succeeds on the next scheduled retry (fake timers), and one where it fails with a 404 and the stream stays closed; `npm test` 3 times (nothing else heavy running), lint, format check, build; CI green.
