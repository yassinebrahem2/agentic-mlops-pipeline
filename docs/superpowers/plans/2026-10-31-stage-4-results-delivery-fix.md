# Stage 4: Results Delivery Fix Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #577. A stored result is delivered to the results observers once, never twice. Today `deliver_undelivered` reads the undelivered rows, calls the observers, then marks them delivered, and two calls for one project can overlap: the startup delivery in `build_services` runs after `scheduler.start()`, and `collect` calls it at the start of every tick. On a slow runner both read the same rows and every observer gets the batch twice (Windows CI shard 3 fails on it).

**Branch:** `plan/stage-4-results-delivery-fix`. **Consumes:** merged code. **Produces:** nothing other plans use; I22 keeps its signatures. The plan PR says `Closes` the plan issue, its task issue and #577.

**Read first:** bug #577; `backend/src/mlops_agents/monitoring/results/service.py` (`deliver_undelivered`, `collect`); `backend/src/mlops_agents/api/services.py` (the startup delivery loop and `scheduler.start()`; `waker.resume` is already placed before the start); the results observers on `main` (`SignalsFromResults`, the two baseline observers, expectations).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One delivery at a time per project | merged code |

---

### Task 1: One delivery at a time per project

**Files:** `backend/src/mlops_agents/monitoring/results/service.py`, `backend/src/mlops_agents/api/services.py` (its startup lines); tests under `backend/tests/monitoring/results/` and the `build_services` wiring tests.

**Notes:**
- **At the cause, in its one owner.** `ResultsService` holds one per-project lock across fetch → observers → mark in `deliver_undelivered`, so two deliveries for one project never overlap; a collection tick that arrives during one waits. The lock does not re-enter: check that no results observer calls back into `collect` or `deliver_undelivered` (today's observers: `SignalsFromResults`, the two baseline observers, expectations). If one does, open a Decision and stop.
- **Startup order.** The startup `deliver_undelivered` loop runs before `scheduler.start()`, beside `waker.resume`, with a `build_services` test of that order.
- **The timing-dependent test** (`test_startup_delivery_real_wiring_order`) is replaced by deterministic ones, without weakening what it proves.

**Tests (stop condition)**, deterministic (events or fakes that hold a step, no sleeps):
- First, a test that fails on `main`: hold an observer in the middle of one delivery, start a second delivery for the same project, release; the observers see exactly one batch, and the rows are marked delivered once.
- A delivery for another project is not held up by the first project's.
- In `build_services`, an undelivered result stored before start is delivered once at startup, before the scheduler's first collection.
- Results tests stay green. Full backend suite, ruff, pyright; CI green (Windows shard 3 included).

When this merges, the lead removes the two known-debt rows it closes (#542's "no test pins the startup-delivery order" and the `api/services.py` startup-delivery row from #542 and #545).

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
