# Stage 4: Results Delivery Fix Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #577. A stored result is delivered to the results observers once, never twice. Today `deliver_undelivered` reads the undelivered rows, calls the observers, then marks them delivered, and two calls for one project can overlap: the startup delivery in `build_services` runs after `scheduler.start()`, and `collect` calls it at the start of every tick. On a slow runner both read the same rows and every observer gets the batch twice (Windows CI shard 3 fails on it).

**Branch:** `plan/stage-4-results-delivery-fix`. **Consumes:** merged code. **Produces:** nothing other plans use; I22 keeps its signatures. The plan PR says `Closes` the plan issue, its task issues, #577 and #586.

**Read first:** bug #577; `backend/src/mlops_agents/monitoring/results/service.py` (`deliver_undelivered`, `collect`); `backend/src/mlops_agents/api/services.py` (the startup delivery loop and `scheduler.start()`; `waker.resume` is already placed before the start); the results observers on `main` (`SignalsFromResults`, the two baseline observers, expectations).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One delivery at a time per project | merged code |
| 2 | An expectation never judges a slot from before it existed (bug #586) | task 1 |

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

---

### Task 2: An expectation never judges a slot from before it existed (bug #586)

Added 2026-10-31: `main` went red on `tests/onboarding/test_onboarding_flow.py::test_the_onboarding_story_survives_a_restart_and_a_second_handoff_kicks_off_again`, depending on the time of day. Bisected to #558: `monitoring/expectations/service.py` `evaluate` (recurring-schedule branch) anchors a never-seen, never-signalled expectation at `now - grace - check_grace_minutes`, before the expectation existed, so the first `check_expectations` can flag a slot missed before onboarding: a false silence alarm that opens an incident and wakes an agent. It was accepted as known debt at #558's merge; a false alarm on a normal path blocks.

**Files:** `monitoring/expectations/store.py` and `service.py`; one new project migration in `backend/src/mlops_agents/storage/migrations/project/` (append-only, the next number) and `storage/tables.py`; tests under `backend/tests/monitoring/expectations/`.

**Notes:**
- **`registered_at`** (lead's decision): a new column on the expectations table, set on the first insert only. A re-seed, the startup reconcile or a handoff upsert never resets it; resetting it would hide a real silence for a slot missed just before a restart.
- `evaluate` never judges a slot before `registered_at`. A slot missed after registration still raises silence.
- **The migration backfills existing rows** with the migration's time, so a project that upgrades raises no false alarm either.
- The column stays inside the store; I25 is unchanged.

**Tests (stop condition)**, with a fixed clock:
- First, one that fails on `main`: a new expectation, and a slot inside the old look-back window: no silence.
- The same across a restart (state persisted).
- A slot missed after registration still raises silence.
- A re-seed keeps `registered_at`.
- The onboarding restart test passes at any time of day (fix the clock, or run it at a time that failed before).
- Full backend suite, ruff, pyright; CI green.

When this merges, the lead removes #558's known-debt item "a freshly seeded, never-seen expectation can raise silence for an occurrence just before seeding". The plan PR also says `Closes #586`.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
