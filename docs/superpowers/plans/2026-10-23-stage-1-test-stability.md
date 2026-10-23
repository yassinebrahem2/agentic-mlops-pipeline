> **Draft.** Missing: an advisor review round with no blocking issues.

# Stage 1 — Test Stability Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Remove the two intermittent test failures found while reviewing stage 1 PRs, by fixing their causes: frontend page tests that time out on a cold or loaded run (#110), and the executor's cancel path hitting `database is locked` (#109). Neither fix may raise a timeout, add a retry or re-run a test.

**Branch:** `plan/stage-1-test-stability`. **Consumes:** merged code only; task 3 also consumes api-sessions task 2's hold gate in `backend/tests/fakes/fake_openai.py`. **Produces:** nothing other plans use.

**Read first:** bug issues #110 and #109 (logs, failing tests, what is already known), the stage overview's global constraints, and `AGENTS.md` Code quality ("No workarounds and no technical debt").

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Frontend page tests that wait on data (#110) | merged code |
| 2 | The executor's cancel path and the write lock (#109) | merged code |
| 3 | Agent-runtime session tests hold turns with the gate | api-sessions task 2 (#56) merged on `main` |

Tasks 1 and 2 are independent and can start now; each gets an early PR (its issue says so), because every PR's CI depends on these flakes being gone. Task 3 starts only once api-sessions' plan PR (#107) is on `main`.

---

### Task 1: Frontend page tests that wait on data (#110)

**Files:** the tests listed in #110 and whatever shared test code or production code the cause lives in (`frontend/src/test/`, the pages' query hooks). Name every file you change in the PR and why.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- First find the cause, with evidence. Measure where the time goes in one failing test on a cold run, for example with `vitest --reporter=verbose` timings and timestamps around the stubbed fetch and the `findBy*` call. Candidates to rule in or out: the first transform of a page's module graph on a cold Vite cache, a stub that answers only after another request settles, real timers or `setTimeout` in a page, and a query that waits on another query. Write the cause in the PR description.
- Fix that cause. Not allowed: raising `asyncUtilTimeout`, `testTimeout` or a `findBy*` timeout; retries; `--sequence` or pool changes made only to hide load.
- If the measured cause is not a defect in our code (the cold transform of a module graph, or plain CPU contention, with the test, its stub and the page all correct), report the measurement in a Decision issue linked to this task and stop: what the suite's setup must do then is the lead's decision.

**Tests (stop condition), from `frontend/`:**
- 10 runs of `npx vitest run`, clearing the caches (`node_modules/.vite` and `node_modules/.vitest` if present) before each one, while the backend suite runs in a loop in another shell for the whole time, ignoring its result (`cd backend && while :; do uv run pytest; done`). All 10 runs pass.
- `npm run lint`, `npm run format:check`, `npm run build` pass; CI green.

---

### Task 2: The executor's cancel path and the write lock (#109)

**Files:** `backend/src/mlops_agents/agents/runtime/` (the executor, the store or the checkpointer handling, wherever the cause is), `backend/tests/agents/runtime/test_executor_cancel.py`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- Find which connection still holds SQLite's write lock when the cancel path writes the turn's end state. #101's review already closes the checkpointer connection before end-state writes and makes store writes finish before a cancel reaches their caller (`shielded.py`); the failure on Linux CI shows that some write is still in flight on another connection at that moment. Prove it with a test that holds that exact situation open deterministically (an event or a held transaction, no sleeps) and fails with `database is locked` before the fix.
- Fix the cause so no end-state write can start while another connection of the same project database holds an uncommitted write. Not allowed: retries on `database is locked`, a longer `busy_timeout`, or catching the error.

**Tests (stop condition), from `backend/`:**
- The new deterministic test fails before the fix and passes after.
- `uv run pytest tests/agents/runtime/test_executor_cancel.py --basetemp=<dir A>` 50 times in a shell loop while full `uv run pytest --basetemp=<dir B>` runs in a loop in another shell, ignoring its result (separate temp folders so the two runs cannot clash): every run passes.
- Full suite, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`; CI green on Linux and Windows.

---

### Task 3: Agent-runtime session tests hold turns with the gate

**Files:** `backend/tests/agents/runtime/test_sessions_stop.py`, `test_sessions_close.py`, `test_sessions_lifecycle.py`, and `session_env.py` if they share setup there.

**Interfaces:** Consumes the hold gate (stage overview, after I7): `FakeState.hold_slow()` and `FakeState.release_slow()` on the `fake_openai` fixture's state. Produces none.

**Implementation notes:**
- Every test here that acts while a `fake-slow` turn is running (Stop, close, close_project, recovery mid-turn) closes the gate (`hold_slow()`) before sending, waits for the hold (the first `text.delta` on its session subscription; nothing else signals it), acts, and asserts on the result. None relies on how long `fake-slow` takes. Tests that need the turn to finish open it (`release_slow()`).
- `session_env.py` sets `fake-slow`'s verification by hand (`record_verification(tool_calling=True, ...)`), which only #106 required. Since #106 is fixed, set it up through `ProvidersService.verify_model` against the fake instead, and remove the hand-written record.

**Tests (stop condition):** the three test files pass 20 times in a shell loop (`--basetemp=<dir A>`) while full `uv run pytest --basetemp=<dir B>` runs in a loop in another shell, ignoring its result; full suite, ruff, format, pyright; CI green on Linux and Windows.
