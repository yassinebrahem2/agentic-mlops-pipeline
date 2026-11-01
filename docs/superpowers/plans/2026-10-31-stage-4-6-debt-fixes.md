# Stages 4 to 6: Debt Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Four tasks (tasks 5 to 8 moved to `2026-11-01-stage-4-6-debt-fixes-2.md` on 2026-11-01), one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** fix the known-debt rows that fail one test: *would a normal action (a restart, a re-onboarding, a person merging, closing or mistyping, a source failing, a second agent, time passing) produce a wrong outcome (a false alarm or wake, a missed page, a lost message, a wrong number, the gate allowing wrongly, a secret in a log)?* The lead re-triaged `docs/known-debt.md` on 2026-10-31 with that test; every other row stays as debt.

**Branch:** `plan/stage-4-6-debt-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; every interface keeps its signature unless a task says so. The plan PR says `Closes` the plan issue and the task issues of tasks 1 to 4; the lead removes each fixed row (or clause) from `docs/known-debt.md` at merge.

**Read first:** `docs/known-debt.md` (each task names its row); `AGENTS.md` (Code quality; the secrets rule).

## Tasks

| Task | Title | Debt row |
|---|---|---|
| 1 | No secret in a log, no agent text outside `untrusted()` | #539, #591 |
| 2 | A merged incident keeps the highest severity and resolves only when every subject is clear; a restart wakes no one again | #545, #547 |
| 3 | A failing observer or hook registration loses nothing | #542, #553 |
| 4 | One active fix per incident; a late training run is still linked | #557, #575 |
| 5 to 8 | moved to the part 2 plan (2026-11-01) | |

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

### Tasks 5 to 8: moved

Moved on 2026-11-01 to their own plan, [`2026-11-01-stage-4-6-debt-fixes-2.md`](2026-11-01-stage-4-6-debt-fixes-2.md) (its tasks 1 to 4), to spread the work before the deadline.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
