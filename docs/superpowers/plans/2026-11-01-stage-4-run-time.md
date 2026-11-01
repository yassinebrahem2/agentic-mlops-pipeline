# Stage 4: A Result's Time Is When Its Check Ran Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, plus an early PR for task 1 (`AGENTS.md` rule 5): the live demo's agents write checks by `RegisterCheck`'s text, so task 1 must reach `main` before task 2's interface work. Each task lists its files, tests (its stop condition) and checks.

**Goal:** bug #645. `ran_at` is the time the check ran, never the date of the data it covers (that goes in `detail`). Today `RegisterCheck` tells an agent "the time the result describes", so a daily check over yesterday's data stamps yesterday's date and raises a false silence every day; and a team check from the handoff must have a cadence, so a check the team runs on demand (after training, by hand) gets an invented schedule it never keeps and a false silence each time. (Decided 2026-11-01 with the user: both parts.) A team check whose records carry no run time cannot be read correctly at all (stamping the time the app reads it is wrong on a first read of a file with history and after a restart), so it is not read: Onboarding leaves it out and says so.

**Branch:** `plan/stage-4-run-time`. **Milestone:** Stage 4. **Consumes:** merged code. **Produces:** `CheckSpec.cadence: Cadence | None = None` (the stage 3 overview's `CheckSpec` line, changed with this plan); `ResultRecord.ran_at`'s one description. The plan PR says `Closes` the plan issue, its task issues and #645.

**Read first:** bug #645 (the repro); the stage 4 spec's "Times" (changed with this plan); `monitoring/results/contract.py` (`ResultRecord`, `ParserMapping`); `agents/tools/register_check.py` (`_RECORD_FIELD_NOTES`, how the record shape is served); `monitoring/results/parser_kind.py` (`CODE_PARSER_CONTRACT`) and where a `parser` proposal is validated; `proposals/payloads.py` (`CheckSpec`); `monitoring/expectations/seed.py`; `onboarding/handoff_preview.py`; `frontend/src/pages/agents/proposals/HandoffSections.tsx`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | `ran_at` is when the check ran, said once, and never invented | merged code |
| 2 | A team check without a schedule is read but never judged silent | task 1 |

---

### Task 1: `ran_at` is when the check ran, said once, and never invented

**Files:** `backend/src/mlops_agents/monitoring/results/contract.py` (`ResultRecord.ran_at`'s `Field(description=...)`); `agents/tools/register_check.py` (its `ran_at` note); `agents/tools/propose.py` (`schema()`); `monitoring/results/parser_kind.py` and the place a `parser` proposal is validated; tests under `backend/tests/monitoring/results/`, `backend/tests/agents/tools/`, `backend/tests/monitoring/expectations/`.

**Notes:**
- **One source.** `ResultRecord.ran_at`'s field description says it: "when the check ran (ISO 8601 with its timezone); the date of the data it covers goes in `detail`, never here: the app judges silence by this time". `RegisterCheck`'s served text and its example read it from the model, and `Propose`'s served description appends it (read from the model) where it already appends `CODE_PARSER_CONTRACT`, since `Propose`'s payload is untyped and no field description reaches the agent through its schema; `_RECORD_FIELD_NOTES` keeps no `ran_at` text of its own.
- **Never invented.** A mapping parser whose `defaults` set `ran_at`, or whose `fields` do not map it, is refused when proposed (a tool error saying the records need the time the check ran, mapped from a field): a constant time is always wrong, and a missing one fails every record. The stage 4 end-to-end fixture maps `ran_at` from its `ts` field (checked 2026-11-01), so it is unaffected. Checked where a `parser` proposal is validated, not as a `ParserMapping` model validator, so a parser already stored still loads.

**PR:** open a PR into `main` for this task as soon as it is done (`AGENTS.md` rule 5), from `plan/stage-4-run-time-task1`.

**Tests (stop condition):** first, one that fails on `main`: `RegisterCheck`'s description and output and `Propose`'s description carry `ResultRecord.ran_at`'s description (read from the model), and the old wording is gone; the repro from #645 as a test: a daily check stamped with its run time meets its occurrence, one stamped with the data's date does not (it pins what the text warns about); a mapping parser with `ran_at` in `defaults` is refused, and one that maps it from a field is accepted; a stored parser with a `ran_at` default still loads; backend suite, ruff, pyright; CI green.

---

### Task 2: A team check without a schedule is read but never judged silent

**Files:** `backend/src/mlops_agents/proposals/payloads.py` (`CheckSpec.cadence`); `agents/tools/propose.py` (`schema()`); `monitoring/expectations/seed.py`; `onboarding/handoff_preview.py`; `agents/definitions/onboarding.md` (one line, pointing at the field's description); `frontend/src/pages/agents/proposals/HandoffSections.tsx` and its test; `frontend/openapi.json` and `schema.d.ts` regenerated (`npm run gen:api`); tests under `backend/tests/proposals/`, `backend/tests/monitoring/expectations/`, `backend/tests/onboarding/`.

**Notes:**
- `CheckSpec.cadence: Cadence | None = None`, with a `Field(description=...)` that is the Onboarding rule, appended to `Propose`'s served description (read from the model, beside `ran_at`'s): "the schedule the team runs the check on; leave it out for a check run on demand (after training, by hand): it is then read, compared with the agents' incidents and counted as the team's baseline, but never judged silent. A team check whose records carry no time they ran cannot be read: leave it out of `checks` and list it under `gaps`, with adding a run time to its records as the next step". A handoff already stored, which always has a cadence, loads unchanged.
- `seed.py` registers no expectation for a check without a cadence, and retires one it had registered when a new handoff drops the cadence (as for a removed key).
- The handoff preview and `HandoffSections` show "not judged for silence" for a check without a cadence.
- Nothing else reads `CheckSpec.cadence` (`mlflow_source.py` reads the check, not its cadence: confirm it).

**Tests (stop condition):** first, one that fails on `main`: `Propose`'s description carries `CheckSpec.cadence`'s description (read from the model); a handoff with a team check without a cadence validates, and seeding it registers no expectation, so no silence is ever raised for it while its records are still read and stored; a handoff that drops a check's cadence retires its expectation; a stored handoff with a cadence loads as before; the preview and the card say "not judged for silence"; frontend tests, lint and format; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
