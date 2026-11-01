# Stages 4 to 6: Debt Fixes, Part 2 Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Four tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5), except task 4, whose change is in the demo repo and gets its own PR there. Each task lists its files, tests (its stop condition) and checks.

**Goal:** the second half of the debt fixes: the known-debt rows that fail one test, *would a normal action (a restart, a re-onboarding, a person merging, closing or mistyping, a source failing, a second agent, time passing) produce a wrong outcome (a false alarm or wake, a missed page, a lost message, a wrong number, the gate allowing wrongly, a secret in a log)?* Moved on 2026-11-01 out of `2026-10-31-stage-4-6-debt-fixes.md` (they were its tasks 5 to 8), word for word, to spread the work before the deadline.

**Branch:** `plan/stage-4-6-debt-fixes-2`. **Consumes:** merged code. **Produces:** nothing other plans use; every interface keeps its signature unless a task says so. The plan PR says `Closes` the plan issue and the task issues of tasks 1 to 3 (task 4's PR in the demo repo closes its own issue with a full cross-repo reference); the lead removes each fixed row (or clause) from `docs/known-debt.md` at merge.

**Read first:** `docs/known-debt.md` (each task names its row); `AGENTS.md` (Code quality; the secrets rule). Task 3 changes `agents/runtime/sessions.py`, which plan #615 (PR #626) also changes: bring `main` in before it.

## Tasks

| Task | Title | Debt row |
|---|---|---|
| 1 | Mistyped on-call addresses, windows and snoozes are refused | #549, #594 |
| 2 | Stats and Usage show the right numbers | #581, #552 |
| 3 | Queued turns after a restart use the one start; a test that can't flake | #574, #543 |
| 4 | The demo's checks never invent a value | #387 (hotel-cancellation-demo) |

Every task: a test that fails on `main` first, then the fix; targeted tests, then the backend suite (or the frontend checks for the frontend parts), ruff, pyright; CI green. Task 4 changes the demo's `drift` and `cancellation_rate`: the demo world's `model_decay` run (Decision #627) recorded company commit `e328674`, so its number 2 is rechecked on the same data after task 4 merges.

---

### Task 1: Mistyped addresses, windows and snoozes are refused (#549, #594)

**Files:** the on-call `PUT` route and the handoff seeder (`notifications/`), the onboarding handoff proposal check (`onboarding/`), the maintenance windows route and the `maintenance` kind's apply, and the snooze route (`monitoring/incidents/`); tests. The contract types (`Contact`, `WindowSpec`) keep their shape; validate where they enter.

**Notes:**
- An email contact's address must be a valid email, a Discord contact's a non-empty user ID. Refuse where a person can fix it: the on-call `PUT` route (422) and the onboarding handoff proposal's check (a warning the person sees before approving). The handoff seeder (which runs `OnCallPeople.replace` at startup and on a stored handoff) never refuses the whole list: it skips the bad contact with a warning, so one typo can't leave a project with no one on call. The email rule has one source (a small regex and its function in `notifications/constants.py`; no new dependency: `EmailStr` needs `email-validator`).
- A maintenance window needs `start < end` and a non-empty scope (422 on the route; the kind's apply refuses with a `DomainError`), so an inert window can't hide the false alarms it was meant to suppress.
- A snooze needs `until` in the future and a live incident (422/409), like `close`.

**Tests:** each refusal; a valid one still works; a handoff with one bad contact seeds the other people and skips that contact with a warning.

---

### Task 2: Stats and Usage show the right numbers (#581, #552)

**Files:** `backend/src/mlops_agents/stats/service.py` (or `metrics.py`); `frontend/src/pages/stats/...`; `frontend/src/pages/usage/format.ts`; tests.

**Notes:**
- `cost_per_incident` divides by the incidents in the window *and* the category filter.
- On the Stats page, "collecting data" (below `STATS_MIN_VERDICTS`) wins over "not available" for verdict-based numbers, so zero verdicts show "collecting data (based on 0)" from the API's own `collecting` and count (no copy of `STATS_MIN_VERDICTS` in the frontend).
- `formatUsd` shows a tiny priced cost as "<$0.0001", never "$0.0000".

**Tests:** a category filter changes `cost_per_incident`; the page with zero verdicts shows "collecting data"; `formatUsd(0.00001)` gives "<$0.0001" and `formatUsd(0)` gives "$0".

---

### Task 3: Queued turns after a restart; a test that can't flake (#574, #543)

**Files:** `backend/src/mlops_agents/agents/runtime/sessions.py` (`get`); `backend/tests/promotion/test_reports.py`; tests.

**Notes:**
- `SessionManager.get` (after a restart) starts queued turns with first-turn Stops on, so a Stop right after a restart can stop a queued or redelivered turn: the #569 symptom. Start them through `start_queued_turns`: a Stop when no turn is running is dropped (stage 1 rule), so a turn started after a restart takes no first-turn Stop.
- `test_reports`' "newest" relies on ULID order within one millisecond: give the reports explicit, increasing times.

**Tests:** after a restart, a Stop that arrives before the queued turn is taken is dropped and the turn runs; the reports test passes 20 runs in a row.

---

### Task 4: The demo's checks never invent a value (#387, hotel-cancellation-demo)

**Files (in `yassinebrahem2/hotel-cancellation-demo`):** `src/hotel_demo/checks/drift.py`, `src/hotel_demo/checks/cancellation_rate.py`; their tests.

**Notes:**
- `drift.py` gives `stat = 0.0` when the current or training sample is empty (~93-94) and uses all bookings when the training window is missing (~70); `cancellation_rate.py` gives `0.0` when `total == 0` (~42). These are the team's own checks, the baseline Stats compares the agents against, so an invented value makes the team look like it raised (or missed) an alert it didn't. On an empty window, report `not_enough_data` and no value (I22's field; check the demo's result writer carries it, and add it there if it doesn't). Note in the PR whether this changes demo-world's `model_decay` pass/fail table (it reads these checks).
- Its own PR in the demo repo (`Closes` this plan's task issue by link); the demo repo's CI green.

**Tests:** an empty window gives `not_enough_data` and no value for each check; a normal window is unchanged.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
