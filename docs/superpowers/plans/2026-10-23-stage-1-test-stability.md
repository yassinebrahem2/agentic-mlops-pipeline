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

**Files:** `backend/tests/agents/runtime/test_executor_cancel.py`, `backend/src/mlops_agents/agents/runtime/executor.py` (one argument and its comment), and the test of the guarantee below (in `test_executor_cancel.py` or `test_graph_checkpointer.py`).

**Interfaces:** Consumes merged code. Produces none.

**Cause (Decision #115):** the lock is the test's own. `test_executor_cancellation_during_slow_tool` opens an uncommitted `INSERT` on the turn's checkpointer connection, a state the runtime cannot produce, and it syncs by polling with `sleep`, so under load it opens that transaction just before the model node's store write, which then fails with `database is locked`. LangGraph's default durability (`"async"`) runs every checkpoint write as a background task that a cancel does not cancel; the run's exit waits for them, so Stop and `close_project` never leave a checkpoint transaction open.

**Implementation notes:**
- Rewrite the cancel test so it models only reachable states: inject no transaction, and wait for the tool to start on an event set in `SlowDummyTool.run`, never by polling. Keep the coverage of `shielded` store writes and of the checkpointer connection closing before end-state writes.
- Make the guarantee part of our code: the executor's `graph.ainvoke(...)` passes `durability="async"` explicitly, with a comment saying why (checkpoint writes must finish even when the turn is cancelled).
- Add a test of that guarantee at the graph run the executor makes (`create_turn_graph` and the same `ainvoke` arguments; the saver it uses is created by the test, no production hook): a checkpoint write in flight when the run is cancelled still commits, and the cancel reaches the caller only after it has. Deterministic: an event inside the test's saver write, no sleeps. Show in the PR that it fails with `durability="sync"`.
- No runtime code for states the app cannot reach (no checkpointer subclass in production, no shield on checkpoint writes).

**Tests (stop condition), from `backend/`:**
- The guarantee test passes, and fails with `durability="sync"` (stated in the PR).
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
