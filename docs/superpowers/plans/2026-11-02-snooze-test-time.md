# Frontend: The Snooze Tests Work on Any Date Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #684. Frontend CI is red on `main` and on every PR: `IncidentList.test.tsx` snoozes an incident until the fixed time `2026-11-02T10:00`, and `SnoozeControl.tsx` shows "Snoozed until" only while the snooze is still in the future, so the test fails since that time passed. `IncidentPage.test.tsx` snoozes to the same fixed time. No product code is wrong.

**Branch:** `plan/snooze-test-time`. **Milestone:** Stage 4: Monitoring and incidents. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #684.

**Read first:** bug #684; `frontend/src/pages/incidents/SnoozeControl.tsx` (the `snoozed` line); the two tests below.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The snooze tests choose a time in the future | merged code |

---

### Task 1: The snooze tests choose a time in the future

**Files:** `frontend/src/pages/incidents/IncidentList.test.tsx`, `frontend/src/pages/incidents/IncidentPage.test.tsx` (their snooze tests only); a helper beside them only if both use it.

**Notes:**
- The chosen time is computed from the current time (for example tomorrow at 10:00, local, as the `datetime-local` input takes it), and the expected request body from that same value; no fixed date. Product code is not changed.

**Tests (stop condition):** both snooze tests pass today and would pass on any later date (check by running them once with the system date moved a year ahead, or by reasoning in the PR why no date can break them); the frontend suite, lint, format and build; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
