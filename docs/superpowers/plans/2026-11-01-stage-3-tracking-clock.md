# Stage 3: Tracking Uses the App's Clock Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #638. The tracking service and its GitHub Actions adapter use the clock `create_app` was given, as every stage 4 to 6 service does, so a sync's `last_sync` and the GitHub runs window follow it. Today `build_services` builds `TrackingService` without the clock and `default_adapters(http)` builds `GithubActionsAdapter` without one, so both read the wall clock; the stage 4 end-to-end test (plan #608) cannot see a run it plants at its fake clock's time, and every scene from the critical page on waits on this.

**Branch:** `plan/stage-3-tracking-clock`. **Milestone:** Stage 3. **Consumes:** merged code. **Produces:** nothing other plans use; `default_adapters` gains a clock parameter (its one caller is `build_services`). The plan PR says `Closes` the plan issue, its task issue and #638.

**Read first:** bug #638; `backend/src/mlops_agents/api/services.py` (where `TrackingService` is built, ~460, and how the other services get `clock`); `tracking/service.py` and `tracking/adapters/{__init__,github_actions,mlflow}.py` (each takes a `Callable[[], datetime]`, not the app's `Clock`); `mlops_agents/clock.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Tracking reads the app's clock | merged code |

---

### Task 1: Tracking reads the app's clock

**Files:** `backend/src/mlops_agents/api/services.py` (the `TrackingService` lines); `backend/src/mlops_agents/tracking/adapters/__init__.py` (`default_adapters`); `tracking/adapters/mlflow.py` only if it reads the time itself; tests under `backend/tests/tracking/` and `backend/tests/api/`.

**Notes:**
- `build_services` passes the app's clock to `TrackingService` and to `default_adapters`, which gives it to every adapter that reads the time (GitHub Actions today; MLflow too if it reads the time anywhere). The adapters keep taking a `Callable[[], datetime]`: pass `clock.now` (one adaptation, in `build_services`), don't change their types.
- No other default: the wall-clock defaults stay only for code that builds these classes outside `build_services` (their own unit tests).

**Tests (stop condition):**
- First, one that fails on `main`: `create_app(clock=<fake clock>)`, a `github_actions` source, a failed workflow run created at the fake clock's time in the fake GitHub, `Scheduler.run_due` at that instant: the run is synced (a `ci` record stored), and the source's `last_sync` is the fake clock's time.
- The MLflow adapter path still syncs under the fake clock.
- Backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
