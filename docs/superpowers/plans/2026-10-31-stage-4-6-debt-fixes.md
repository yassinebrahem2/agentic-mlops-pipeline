# Stages 4 to 6: Debt Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Eight tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5), except task 8, whose change is in the demo repo and gets its own PR there. Each task lists its files, tests (its stop condition) and checks.

**Goal:** fix the known-debt rows that fail one test: *would a normal action (a restart, a re-onboarding, a person merging, closing or mistyping, a source failing, a second agent, time passing) produce a wrong outcome (a false alarm or wake, a missed page, a lost message, a wrong number, the gate allowing wrongly, a secret in a log)?* The lead re-triaged `docs/known-debt.md` on 2026-10-31 with that test; every other row stays as debt.

**Branch:** `plan/stage-4-6-debt-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every interface keeps its signature unless a task says so. The plan PR says `Closes` the plan issue and the task issues of tasks 1 to 7 (task 8's PR in the demo repo closes its own issue with a full cross-repo reference); the lead removes each fixed row (or clause) from `docs/known-debt.md` at merge.

**Read first:** `docs/known-debt.md` (each task names its row); `AGENTS.md` (Code quality; the secrets rule).

## Tasks

| Task | Title | Debt row |
|---|---|---|
| 1 | No secret in a log, no agent text outside `untrusted()` | #539, #591 |
| 2 | A merged incident keeps the highest severity and resolves only when every subject is clear; a restart wakes no one again | #545, #547 |
| 3 | A failing observer or hook registration loses nothing | #542, #553 |
| 4 | One active fix per incident; a late training run is still linked | #557, #575 |
| 5 | Mistyped on-call addresses, windows and snoozes are refused | #549, #594 |
| 6 | Stats and Usage show the right numbers | #581, #552 |
| 7 | Queued turns after a restart use the one start; a test that can't flake | #574, #543 |
| 8 | The demo's checks never invent a value | #387 (hotel-cancellation-demo) |

Every task: a test that fails on `main` first, then the fix; targeted tests, then the backend suite (or the frontend checks for the frontend parts), ruff, pyright; CI green.

---

### Task 1: No secret in a log, no agent text outside `untrusted()` (#539, #591)

**Files:** `backend/src/mlops_agents/onboarding/kinds_common.py`; `backend/src/mlops_agents/monitoring/incidents/follow_ups.py`; tests.

**Notes:**
- `kinds_common.py` (~43, ~56) calls `logger.exception` on any non-`DomainError`, and the traceback can carry a connection's or tracking auth's credential. Log through the existing scrubbing (`error_text` with the part's secret values, as the stored reason already does) and without the raw traceback's arguments.
- The follow-up wake text embeds the agent-written `note` raw: wrap it in `untrusted(..., source="follow_up")`.

**Tests:** an apply that fails with an exception whose text contains a credential leaves the credential out of the log (caplog); the follow-up wake text holds the note inside the untrusted tag.

---

### Task 2: Merged incidents, resolve, and the restart re-wake (#545, #547)

**Files:** `backend/src/mlops_agents/monitoring/incidents/service.py`, `dispatch.py`; tests.

**Notes:**
- `merge`: the winner takes the highest severity of the merged incidents and records the severity change event. A merge-driven rise pages no one again: people already paged for an absorbed incident count as paged for the winner (escalation's "already sent" set includes the absorbed incidents' rows), and escalation carries on from where it was, not from step 0.
- Resolve (`update(status="resolved")`) checks every distinct (subject, model) among the incident's signals still clear, not only the first signal's.
- `DiagnosisDispatch` keeps the previous diagnoser in memory, so after a restart a `failed_training` incident (subtype) can be woken again for `retraining` under a new key. Derive the previous diagnoser from the stored incident (category and subtype) instead.

**Tests:** merging a critical into a medium gives a critical winner and a severity change event, with no duplicate page to anyone already paged for the critical and escalation continuing; an incident with two subjects does not resolve while the second still fires; after a restart (a fresh `DiagnosisDispatch`), an unchanged subtype wakes nobody again.

---

### Task 3: A failing observer or hook registration loses nothing (#542, #553)

**Files:** `backend/src/mlops_agents/monitoring/results/service.py` (`deliver_undelivered`); `backend/src/mlops_agents/monitoring/hooks/handoff.py` (`HandoffHooks.stored`); tests.

**Notes:**
- Today an observer that raises still gets its records marked delivered, so a fired result never opens an incident. Mark a row delivered only after every observer returned. One that raises leaves the row undelivered for the next tick (I22 already requires idempotent observers), with an attempt counter on the row (one new append-only project migration after `0008`): after `RESULT_DELIVERY_MAX_ATTEMPTS` (5, in `monitoring/results/constants.py`) failures the row is marked delivered and logged at ERROR, so a permanently failing observer can't wedge delivery or resend everything forever. `fetch_undelivered` reads a bounded page.
- `HandoffHooks.stored`: one failing `register` stops the rest and the dropped-hook disabling, so a command the team removed stays enabled. Register each hook in its own try, log a failure, and always run the disabling.

**Tests:** an observer that raises once: on the next delivery the incident still opens, and the other observers' effects happen once (one incident, one baseline link), even though they receive the row again; a permanently failing observer is given up after `RESULT_DELIVERY_MAX_ATTEMPTS` while newer results keep flowing. One failing `register` among three: the other two register and a dropped hook is disabled.

---

### Task 4: One active fix per incident; a late run is still linked (#557, #575)

**Files:** `backend/src/mlops_agents/training/fixes.py`, `linking.py`; tests.

**Notes:**
- `request()`: one active fix per incident is enforced by a unique partial index on `fix_requests` (incident, active statuses) with an `IntegrityError` fallback to the existing fix (a plain deferred SQLite transaction doesn't stop two readers both inserting), so two agents requesting a fix on one incident get one fix. Definitions task 2 grants `RequestFix` to several diagnosing agents, so this becomes a normal path.
- A request marked `silent` (its `training_request` expectation passed) is never linked by a run that arrives later, so the fix never ends. A late run with the request's tag still links (status `linked`), retires the expectation, and the fix goes on as for an on-time run. The link also clears the silence it raised, through expectations' existing "seen" path (`note_seen`), so the silence incident can resolve (today resolve refuses with `incident_still_firing` while the expectation is silent).

**Tests:** two concurrent `request()` calls for one incident create one fix; a run tagged with a `silent` request links it, the fix ends `done`, and the silence incident can then resolve.

---

### Task 5: Mistyped addresses, windows and snoozes are refused (#549, #594)

**Files:** the on-call `PUT` route and the handoff seeder (`notifications/`), the onboarding handoff proposal check (`onboarding/`), the maintenance windows route and the `maintenance` kind's apply, and the snooze route (`monitoring/incidents/`); tests. The contract types (`Contact`, `WindowSpec`) keep their shape; validate where they enter.

**Notes:**
- An email contact's address must be a valid email, a Discord contact's a non-empty user ID. Refuse where a person can fix it: the on-call `PUT` route (422) and the onboarding handoff proposal's check (a warning the person sees before approving). The handoff seeder (which runs `OnCallPeople.replace` at startup and on a stored handoff) never refuses the whole list: it skips the bad contact with a warning, so one typo can't leave a project with no one on call. The email rule has one source (a small regex and its function in `notifications/constants.py`; no new dependency: `EmailStr` needs `email-validator`).
- A maintenance window needs `start < end` and a non-empty scope (422 on the route; the kind's apply refuses with a `DomainError`), so an inert window can't hide the false alarms it was meant to suppress.
- A snooze needs `until` in the future and a live incident (422/409), like `close`.

**Tests:** each refusal; a valid one still works; a handoff with one bad contact seeds the other people and skips that contact with a warning.

---

### Task 6: Stats and Usage show the right numbers (#581, #552)

**Files:** `backend/src/mlops_agents/stats/service.py` (or `metrics.py`); `frontend/src/pages/stats/...`; `frontend/src/pages/usage/format.ts`; tests.

**Notes:**
- `cost_per_incident` divides by the incidents in the window *and* the category filter.
- On the Stats page, "collecting data" (below `STATS_MIN_VERDICTS`) wins over "not available" for verdict-based numbers, so zero verdicts show "collecting data (based on 0)" from the API's own `collecting` and count (no copy of `STATS_MIN_VERDICTS` in the frontend).
- `formatUsd` shows a tiny priced cost as "<$0.0001", never "$0.0000".

**Tests:** a category filter changes `cost_per_incident`; the page with zero verdicts shows "collecting data"; `formatUsd(0.00001)` gives "<$0.0001" and `formatUsd(0)` gives "$0".

---

### Task 7: Queued turns after a restart; a test that can't flake (#574, #543)

**Files:** `backend/src/mlops_agents/agents/runtime/sessions.py` (`get`); `backend/tests/promotion/test_reports.py`; tests.

**Notes:**
- `SessionManager.get` (after a restart) starts queued turns with first-turn Stops on, so a Stop right after a restart can stop a queued or redelivered turn: the #569 symptom. Start them through `start_queued_turns`: a Stop when no turn is running is dropped (stage 1 rule), so a turn started after a restart takes no first-turn Stop.
- `test_reports`' "newest" relies on ULID order within one millisecond: give the reports explicit, increasing times.

**Tests:** after a restart, a Stop that arrives before the queued turn is taken is dropped and the turn runs; the reports test passes 20 runs in a row.

---

### Task 8: The demo's checks never invent a value (#387, hotel-cancellation-demo)

**Files (in `yassinebrahem2/hotel-cancellation-demo`):** `src/hotel_demo/checks/drift.py`, `src/hotel_demo/checks/cancellation_rate.py`; their tests.

**Notes:**
- `drift.py` gives `stat = 0.0` when the current or training sample is empty (~93-94) and uses all bookings when the training window is missing (~70); `cancellation_rate.py` gives `0.0` when `total == 0` (~42). These are the team's own checks, the baseline Stats compares the agents against, so an invented value makes the team look like it raised (or missed) an alert it didn't. On an empty window, report `not_enough_data` and no value (I22's field; check the demo's result writer carries it, and add it there if it doesn't). Note in the PR whether this changes demo-world's `model_decay` pass/fail table (it reads these checks).
- Its own PR in the demo repo (`Closes` this plan's task issue by link); the demo repo's CI green.

**Tests:** an empty window gives `not_enough_data` and no value for each check; a normal window is unchanged.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
