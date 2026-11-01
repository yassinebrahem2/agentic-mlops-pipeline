# Stage 2: The Event Log Uses the App's Clock Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #641. `SqliteEventLog.record` stamps every event's `ts` with the wall clock (`now_iso()`, `events/log.py`), and `build_services` builds it without the clock `create_app` was given (`api/services.py`, ~320). Under a fake clock (the end-to-end tests, the Stage 7 replay) every event carries the real date, so anything that reads events by time (Stats' sandbox failures span in `stats/sources.py`, the activity feed's order against other records) disagrees with the clock. The same shape as #638.

**Branch:** `plan/stage-2-event-clock`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** nothing other plans use; `SqliteEventLog` gains a clock parameter (its one production caller is `build_services`). The plan PR says `Closes` the plan issue, its task issue and #641.

**Read first:** bug #641; `backend/src/mlops_agents/events/log.py`; `api/services.py` (where `SqliteEventLog` is built, and how the other services get `clock`); `mlops_agents/clock.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Events are stamped with the app's clock | merged code |

---

### Task 1: Events are stamped with the app's clock

**Files:** `backend/src/mlops_agents/events/log.py`; `backend/src/mlops_agents/api/services.py` (the `SqliteEventLog` line); tests under `backend/tests/events/` and `backend/tests/api/`.

**Notes:**
- `SqliteEventLog` takes the app's `Clock` (as the stage 4 to 6 services do) and stamps `ts` from it; `build_services` passes its `clock`. A default for direct construction stays only for code that builds it outside `build_services` (its own unit tests).
- Only the event log: other wall-clock reads are out of this task (note any you see on `build_services`'s path in the PR, fix none).

**Tests (stop condition):** first, one that fails on `main`: `create_app(clock=<fake clock>)` at an instant far from now, an action that records an event (a tracking sync through `Scheduler.run_due`, or any route that records one), the event's `ts` is the fake clock's time; events still come back in order; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
