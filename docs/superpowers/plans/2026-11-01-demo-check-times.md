# Demo: Check Results Carry the Time They Ran Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task. The change is in the demo repo (`yassinebrahem2/hotel-cancellation-demo`): one PR there for the branch, whose description closes this plan's issue and its task issue with full cross-repo references. The task lists its files, tests (its stop condition) and checks.

**Goal:** every line the company's monitoring writes to `monitoring/<date>.jsonl` carries `ran_at`, the real UTC time the check ran. Today a line is `{check, fired, value, threshold, detail[, not_enough_data]}` with no time at all, so no parser can give the app a correct `ran_at` (a mapping parser can only set a constant; a code parser can only take the business date from the file name). The app runs on the real clock and judges silence, detection delay and the recent-changes window by `ran_at` (stage 4 spec, "Times"), so a business date there gives a false silence alarm on every check each real day, a detection delay of years on the Stats page, and no PR ever linked to an incident. Data feeds are not affected (the app reads their files' modification times).

**Branch:** `plan/demo-check-times`, in the demo repo. **Milestone:** Demo project. **Consumes:** the demo repo's `main`. **Produces:** the monitoring line with `ran_at`, in the demo overview's data folder layout (`2026-10-27-demo-overview.md`, changed with this plan). Stage 7's agents plan consumes it.

**Read first:** the demo overview's "Data folder layout" and its rules on times; `src/hotel_demo/checks/{accuracy,cancellation_rate,drift}.py` (each writes its own line today, ~95-105); `src/hotel_demo/daily.py` (how checks are discovered and run, and how `jobs` lines get their real times); `tests/test_checks.py`, `tests/test_daily.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Every check line carries the real time it ran | the demo repo's `main` |

---

### Task 1: Every check line carries the real time it ran

**Files (in `yassinebrahem2/hotel-cancellation-demo`):** a new `src/hotel_demo/monitoring.py` (the one writer, outside `checks/` so `daily`'s discovery is untouched); `src/hotel_demo/checks/{accuracy,cancellation_rate,drift}.py` (call it); `tests/test_checks.py`, `tests/test_daily.py`; `README.md` (the monitoring line).

**Notes:**
- **One writer.** `monitoring.py` owns the monitoring line: one function that takes the business date and the check's record, stamps `ran_at` (the real time, `datetime.now(UTC)`, ISO-8601 with its offset, taken when the line is written), and appends the line in one write to `monitoring/<date>.jsonl`. The three checks call it instead of writing the file themselves. The line's other fields stay exactly as they are.
- **The README says it, for whoever adds a check** (the agents read the demo repo, not this repo's overview): a monitoring line's fields, that `ran_at` is the real UTC time the check ran (the business date is the file's name), and that a new check writes its line through `hotel_demo.monitoring`.
- A replayed day appends its lines again, as today (the demo-world README documents this).
- Nothing under `bench/demo-world` reads monitoring lines (checked 2026-11-01), so the added field breaks no reader there.

**Tests (stop condition):** every line each check writes carries `ran_at`, a timezone-aware UTC time within the test's run, parsed back from the file; the line is otherwise unchanged (the existing shape tests, with `ran_at` added); running a day twice appends a second line; `daily` on the fixture still runs exactly the three checks. The demo repo's checks and CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
