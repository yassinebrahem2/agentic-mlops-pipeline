# Stage 1 — Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix small defects found in review: a timing assertion and inline lint suppressions in api-sessions' test code (#119), the board automation moving a closed task when a Decision closes (#121). (#117 and #125 moved to the frontend-fixes plan.)

**Branch:** `plan/stage-1-fixes`. **Consumes:** merged code only. **Produces:** nothing other plans use.

**Read first:** bug issues #119 and #121, and `AGENTS.md` Code quality.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Session live test: no timing assertion, no inline suppressions (#119) | merged code |
| 2 | Board: a Decision never moves a closed task (#121) | merged code |

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
