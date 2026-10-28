# Stage 2 — Stream Tests Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks. Use systematic debugging: the cause is not known yet, and finding it is the task.

**Goal:** Fix bug #377: five tests of the live event stream time out intermittently on the Backend Windows CI job, on branches that do not touch them, so about every other Windows run fails and every merge waits for a rerun.

**Branch:** `plan/stage-2-stream-tests-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug #377 (the five tests, the runs and the teardown logs); `AGENTS.md` Code quality ("No workarounds and no technical debt. Fix the cause, not the symptom.").

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Find and fix the cause of the Windows stream test timeouts (#377) | merged code |

---

### Task 1: Find and fix the cause of the Windows stream test timeouts (#377)

**Files:** `backend/tests/api/conftest.py` (the `live_app` and `live_client` fixtures), `backend/tests/api/test_sessions_stream.py`, `backend/tests/api/test_sessions_live.py`, `backend/tests/events/test_hub.py`, and their helpers; on the app side, only where the cause is: `agents/runtime/hub.py`, `events/` (the hub and the SSE route), `api/routers/` (the session stream route), the app lifespan. `agents/runtime/hub.py` is in rate-limits task 2's file list: a change there touches only the functions the cause is in, and says so in the plan issue. A change to any other file needs a Decision issue first.

**Implementation notes:**
- **Measure first.** On Windows, loop the three test files (not the whole suite, which is too slow locally) and record how often each of the five tests fails, before changing anything: for example `for i in $(seq 30); do uv run pytest tests/api/test_sessions_stream.py tests/api/test_sessions_live.py tests/events/test_hub.py -q -p no:randomly; done`, counting failures. If it does not fail locally, raise the load the way CI has it (run them alongside the rest of `tests/api`) until it does, and write down how. A cause that cannot be reproduced cannot be shown fixed.
- **Find the cause.** All five fail on the end or the resumption of a stream: a client disconnect, an app shutdown with an open stream, a project close, a reconnect with `Last-Event-ID`, a Stop with partial output. The teardown logs show `Event loop is closed`, `timeout graceful shutdown exceeded` and `sqlite3.OperationalError: Connection closed`. Start with the live-server fixture's lifecycle and the order in which the client, the uvicorn server, the hub subscriptions and the databases are torn down, and with how an SSE response notices a disconnect on Windows' event loop. That is where to look, not a diagnosis: write down the cause you prove (a test that fails before the fix and passes after, or a measured before and after) in the plan issue.
- **Not allowed** (each is a workaround): raising a timeout without a measured cause, skipping or `xfail` on Windows, retries or a rerun plugin, sleeps, weakening an assertion. If the cause is in the test harness, fixing the harness is fixing the cause; if it is in the app (a stream that does not end on disconnect, a shutdown that waits), fix the app, since users on Windows hit it too.
- The PR description states the cause in plain words, the measured before and after, and every file changed.

**Tests (stop condition):**
- The loop above, on Windows: before (the failure rate) and after (30 runs, 0 failures), both pasted in the PR.
- A regression test for the cause where one can be written (for example: a disconnect, a shutdown or a project close ends the stream and the subscription within a bound, asserted with an event, not a sleep).
- Five consecutive green `backend (windows-latest)` jobs on the PR's head (rerun the job with `gh run rerun <run-id> --job <job-id>`; no CI edit), plus every other CI job green.
- `uv run pytest`, `ruff check`, `ruff format`, `pyright`.
