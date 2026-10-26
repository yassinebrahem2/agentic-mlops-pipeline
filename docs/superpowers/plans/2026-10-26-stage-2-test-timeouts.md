# Stage 2 — Test Timeouts Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** A hung test or CI job fails on its own, naming where it hung, instead of waiting until someone notices. Every agent gets this from the repo's config, not from its prompt.

**Branch:** `plan/stage-2-test-timeouts`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** `backend/pyproject.toml` (`[tool.pytest.ini_options]`), `backend/tests/conftest.py`, `.github/workflows/*.yml`, `AGENTS.md` (Backend commands), and pytest-timeout's README (methods, `timeout_func_only`, markers).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Every test and CI job has a time limit | merged code |

---

### Task 1: Every test and CI job has a time limit

**Files:** `backend/pyproject.toml`, `backend/uv.lock` (regenerated with `uv lock`, never edited), `backend/tests/conftest.py`, a new `backend/tests/test_timeouts.py`, `.github/workflows/backend.yml`, `e2e.yml`, `frontend.yml`, `package.yml`, `AGENTS.md` (one line).

**Implementation notes:**
- Add `pytest-timeout` to the dev dependencies and `timeout = 120` to `[tool.pytest.ini_options]`. Do **not** set `timeout_method`: the plugin's default uses the signal method where SIGALRM exists (Linux: only the hung test fails, the run goes on) and the thread method on Windows (the run stops with every thread's stack). Forcing `thread` would abort whole Linux runs.
- 120 s, not tighter: the slowest test is about 4 s on an idle machine, Windows CI is about 3× slower, and machines often run several suites at once (`project_knowledge.md`, #120). The limit catches hangs, not slow tests.
- In `tests/conftest.py`, a `pytest_collection_modifyitems` hook gives every `docker`-marked item `pytest.mark.timeout(1200)` (image builds may take up to 900 s) and every `live`-marked item `pytest.mark.timeout(600)`, unless the item already has its own `timeout` marker. This holds when an agent runs `-m docker` locally, not only in CI.
- CI `timeout-minutes` per job, about 3× the slowest measured run or more: `backend` 30, `docker` 30, `e2e` 15, `frontend` 15, `package` and `install` 10.
- `AGENTS.md`, under Backend commands, one sentence: "Run test and build commands with your tool's command time limit." Never name `timeout`: on PowerShell and cmd that is the Windows sleep command.

**Tests (stop condition):**
- `tests/test_timeouts.py` uses `pytester.runpytest_subprocess(..., timeout=60)` (a subprocess, so a Windows thread-method abort can't kill the outer run, and the outer run's own alarm isn't overwritten) on a generated test file with `-o timeout=2`:
  - a test that awaits an event that is never set, using an async fixture whose teardown also awaits a never-set event: the inner run ends within the outer 60 s, its output names the hung test, and it reports a failure (Linux) or a timeout abort with a stack dump (Windows);
  - a `docker`-marked item gets the 1200 s limit (check the collected item's marker, without running it).
- `uv lock` resolves `pytest-timeout` with pytest 9.x (state the version in the PR).
- The full suite passes on Linux and Windows CI; each workflow has `timeout-minutes` on every job.
- Merge order: the `uv.lock` change conflicts with open PRs that also change it. Open this PR after #250 and #266 are merged, or warn their owners in the plan issue.
