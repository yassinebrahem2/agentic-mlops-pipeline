# Stage 1 — Test Stability Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Remove the two intermittent test failures found while reviewing stage 1 PRs by fixing their causes: the executor's cancel path hitting `database is locked` (#109), and frontend page tests that time out on a cold or loaded run (#110). Then make the agent-runtime session tests independent of timing. No fix may raise a timeout, add a retry or re-run a test.

**Branch:** `plan/stage-1-test-stability`. **Consumes:** merged code; task 3 also consumes the hold gate (stage overview, after I7). **Produces:** nothing other plans use.

**Read first:** bug issues #109 and #110 (logs, failing tests, what is already known), the stage overview's global constraints, and `AGENTS.md` Code quality ("No workarounds and no technical debt").

**Load loops.** The stop conditions run tests while another test run loads the machine. The loops are written for bash; use the equivalent loop in your shell. The load loop ignores its own result (`while :; do ...; done`), so a failure there never ends the load early. Two pytest runs at once each get their own `--basetemp`, and pytest deletes that folder first: use two new folders under the system temp folder (`$TMP/test-stability-a` and `$TMP/test-stability-b`), never a folder inside the repository, your home folder or `MLOPS_AGENTS_HOME`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The executor's cancel path and the write lock (#109) | merged code |
| 2 | Frontend page tests that wait on data (#110) | merged code |
| 3 | Agent-runtime session tests hold turns with the gate | the hold gate (api-sessions task 2, #56) on `main` |

Tasks 1 and 2 start now and each gets an early PR (its issue says so), because every PR's CI depends on these failures being gone. Task 2's branch is cut after task 1, so task 1's PR merges first. Task 3 starts once api-sessions' plan PR (#107) is on `main`.

---

### Task 1: The executor's cancel path and the write lock (#109)

**Files:** `backend/src/mlops_agents/agents/runtime/` (the executor, the store or the checkpointer handling, wherever the cause is), `backend/tests/agents/runtime/test_executor_cancel.py`.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- Find which connection still holds SQLite's write lock when the cancel path writes the turn's end state. #101's review already closes the checkpointer connection before end-state writes and makes store writes finish before a cancel reaches their caller (`shielded.py`); the failure on Linux CI shows some write is still in flight on another connection at that moment. Prove it with a test that holds that exact situation open deterministically (an event or a held transaction, no sleeps) and fails with `database is locked` before the fix.
- Fix the cause so no end-state write can start while another connection to the same project database holds an uncommitted write. Not allowed: retries on `database is locked`, a longer `busy_timeout`, catching the error.
- If you cannot pin the cause down with evidence, report what you measured in a Decision issue linked to this task and stop.

**Tests (stop condition), from `backend/`:**
- The new deterministic test fails before the fix and passes after.
- `uv run pytest tests/agents/runtime/test_executor_cancel.py --basetemp=$TMP/test-stability-a` 50 times in a loop while `uv run pytest --basetemp=$TMP/test-stability-b` runs in a load loop: all 50 pass.
- Full suite, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`; CI green on Linux and Windows.

---

### Task 2: Frontend page tests that wait on data (#110)

**Files:** the tests listed in #110 and whatever shared test code or production code the cause lives in (`frontend/src/test/`, the pages' query hooks). Name every file you change in the PR and why.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- First find the cause, with evidence. Measure where the time goes in one failing test on a cold run, for example `vitest --reporter=verbose` timings and timestamps around the stubbed fetch and the `findBy*` call. Candidates to rule in or out: the first transform of a page's module graph on a cold Vite cache, a stub that answers only after another request settles, real timers or `setTimeout` in a page, a query that waits on another query. Write the cause in the PR description.
- Fix that cause. Not allowed: raising `asyncUtilTimeout`, `testTimeout` or a `findBy*` timeout; retries; pool or sequence settings changed only to hide load.
- If the measured cause is not a defect in our code (the cold transform of a module graph, or plain CPU contention, with the test, its stub and the page all correct), report the measurement in a Decision issue linked to this task and stop: what the suite's setup must do then is the lead's decision.

**Tests (stop condition), from `frontend/`:**
- 10 runs of `npx vitest run`, clearing the caches (`node_modules/.vite` and `node_modules/.vitest` if present) before each one, while `cd backend && uv run pytest --basetemp=$TMP/test-stability-b` runs in a load loop: all 10 pass.
- `npm run lint`, `npm run format:check`, `npm run build` pass; CI green.

---

### Task 3: Agent-runtime session tests hold turns with the gate

**Files:** every test file under `backend/tests/agents/runtime/` that runs a `fake-slow` turn (on `main` today: `test_sessions.py`, `test_sessions_stop.py`, `test_sessions_close.py`, `test_sessions_lifecycle.py`, and the files that build their setup with `session_env.py`), and `session_env.py`.

**Interfaces:** Consumes the hold gate: `FakeState.hold_slow()` and `FakeState.release_slow()` on the `fake_openai` fixture's state. Produces none.

**Implementation notes:**
- Every test that acts while a `fake-slow` turn is running (Stop, close, close_project, recovery mid-turn) calls `hold_slow()` before sending, waits for the hold (the first `text.delta` on its session subscription; nothing else signals it), acts, and asserts on the result. None relies on how long `fake-slow` takes. Tests that need the turn to finish call `release_slow()`.
- `session_env.py` sets `fake-slow`'s verification by hand (`record_verification(tool_calling=True, ...)`), which only #106 required. Set it up through `ProvidersService.verify_model` against the fake instead and remove the hand-written record. Verification sends one probe request to the fake, so a test that counts the fake's requests accounts for it.

**Tests (stop condition):** the runtime session tests pass 20 times in a loop (`--basetemp=$TMP/test-stability-a`) while `uv run pytest --basetemp=$TMP/test-stability-b` runs in a load loop; full suite, ruff, format, pyright; CI green on Linux and Windows.
