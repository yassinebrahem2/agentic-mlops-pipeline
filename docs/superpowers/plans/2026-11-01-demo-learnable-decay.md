# Demo World: A Decay a Retrain Can Learn Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** Decision #627's follow-up. The `model_decay` scenario passes numbers 1 and 2 but the company's own retrain recovers it only partly (February 0.571 against 0.756), so the live demo's "retrain, then promote" step shows a retrain that barely helps. Find a scenario shape the company's pipeline can learn, measured offline first, and change the default only if one lands. **Time box: if no shape lands by the end of 2026-11-03, close this plan with its measurements; the demo keeps the current scenario and its story ("a retrain helps only partly; the diagnosis points at the features").**

**Branch:** `plan/demo-learnable-decay`. **Milestone:** Demo project. **Consumes:** `bench/demo-world` on `main` (#533 merged). **Produces:** nothing other plans use (the Stage 7 agents run does not inject `model_decay`). The plan PR says `Closes` the plan issue and its task issue.

**Read first:** Decision #627 (the measurements and the diagnosis: the retrain learns the rule on its training rows but does not carry it to February; the calendar features explain 0.04 to 0.08 of the gap; the ramp leaves training rows only partly under the rule); `docs/superpowers/plans/2026-10-27-demo-world.md` (task 4 and its numbers); `bench/demo-world/src/demo_world/model_decay.py`, `scenarios/default.toml`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Measure the candidate shapes; adopt one only if it lands | #533 merged |

---

### Task 1: Measure the candidate shapes; adopt one only if it lands

**Files:** `bench/demo-world/src/demo_world/model_decay.py` and `scenarios/default.toml` (only if a shape is adopted, and only the parameters or the rule's shape); `bench/demo-world/src/demo_world/answer_key.py` (flaw 4's text, if adopted); `bench/demo-world/README.md`; tests under `bench/demo-world/tests/`.

**Notes:**
- **Candidates, in this order, each measured offline the way #627's diagnosis was** (scratch copies, the company's own `train` and scoring, nothing committed): (a) no ramp: the share is at its maximum from the window's start; (b) a window that starts earlier, so the calendar weeks the model is scored on appear in training under the rule; (c) a coarser rule: a larger group (all `Online TA`, or all no-deposit bookings) and a round cut (for example 30 days). A shape lands when numbers 1 and 2 still pass and a fresh `train --cutoff 2016-12-15` scores February within 0.05 of the same command on task 3's data.
- Take the first shape that lands; then one honest full run with it, and nothing tuned after. If none lands by the time box, change no code: the PR (or a comment on the plan issue, if nothing is committed) reports every candidate's numbers, and the plan closes.
- The company's code is never changed (the shift is the world's, never a company feature); the rule still reads only the booking's own columns.

**Tests (stop condition):** if a shape is adopted: its rule's tests (followers on both sides of the cut, the share as the scenario says, a rerun picks the same bookings), the answer-key line, demo-world's suite and CI green, and the PR body has the full run's table of numbers 1 to 3 (number 3 now pass or fail again). If none is adopted: the measurements table on the plan issue.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
