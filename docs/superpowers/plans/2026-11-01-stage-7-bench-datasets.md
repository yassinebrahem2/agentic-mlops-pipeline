# Stage 7: Bench Datasets Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** the synthetic drift and the pipeline telemetry datasets, each replayed through the core's replay and reported. Spec: `docs/superpowers/specs/2026-11-01-stage-7-benchmarks-design.md` ("The datasets and their scenarios"). Shared rules and interfaces: `docs/superpowers/plans/2026-11-01-stage-7-overview.md`.

**Branch:** `plan/stage-7-bench-datasets`. **Milestone:** Stage 7: Developer benchmarks. **Consumes:** bench core task 4's early PR (`Dataset`, `checks`, `replay`, `bench replay`). **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue and its task issues.

**Read first:** the spec; the overview; `bench/benchmarks/src/benchmarks/{datasets/contract,checks,replay}.py` and core task 4's fixture dataset under `bench/benchmarks/tests/` (a dataset built on the interface); `backend/src/mlops_agents/monitoring/expectations/service.py` (how a feed's silence is judged: file modification times against its cadence and grace) and `monitoring/results/` (what makes a parser failure).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Synthetic drift | core task 4 |
| 2 | Pipeline telemetry | core task 4 |

---

### Task 1: Synthetic drift

**Files:** `bench/benchmarks/src/benchmarks/datasets/drift.py`; `bench/benchmarks/scenarios/drift.toml`; `bench/benchmarks/pyproject.toml` (`river`, BSD 3-Clause) and `uv.lock`; its tests.

**Notes:**
- 90 business days; rows from river's `ConceptDriftStream` (SEA or Agrawal; the seed is the run's `--seed`, default fixed and recorded) for P(y|x) changes, and a seeded covariate shift of chosen features for P(x) changes. Every drift point (its position and width) is a scenario entry that appends its `Record` (`drift` for P(x), `model_performance` for P(y|x)), plus null spikes (`data_quality`).
- At least 5 problems in each category the scenario reports, 3 days each, each starting at least 9 days after the previous one of its category ends (so match spans never overlap; a test checks the dates), in the 70 days after training. Injections are defined in `drift.py`; `DATASETS` gets its `drift` line.
- The model is trained on the first 20 days, as ULB's is; the checks are the core's fixed set.
- Run it once for real and put its `report.md` in the plan PR's description.

**Tests (stop condition):** the same seed gives the same rows and key; a drift point changes the distribution only from its position on (a test on the generated frame); the scenario's lines are appended once; a short replay (a few days, one drift point) through `replay` opens a `drift` incident scored `real_issue`; CI green (no long run in CI).

---

### Task 2: Pipeline telemetry

**Files:** `bench/benchmarks/src/benchmarks/datasets/telemetry.py`; `bench/benchmarks/scenarios/telemetry.toml`; its tests.

**Notes:**
- 60 business days of a generated batch pipeline (`DATASETS` gets its `telemetry` line): daily feeds (data files) and job output files, with a handoff that declares the feeds with their cadence and grace and a results location for the jobs' records. No public dataset is used (the user agreed, 2026-11-01).
- Injections, each through the product's real operational path and each appending its `Record` (`operational`): a feed that stops arriving for some days (no file written: silence); files that arrive after the feed's deadline plus grace (the file's modification time set late in fake time); a results file the parser cannot read (parser failure). At least 5 problems in total, each starting at least 9 days after the previous one ends. No failed CI or training run (that needs a tracking source, whose fakes are test-only; the spec leaves it out).
- The replay already sets every written file's modification time to its day's start; a late file is written with a later fake time (`os.utime` after the replay's own, past the feed's deadline plus grace).
- Run it once for real and put its `report.md` in the plan PR's description.

**Tests (stop condition):** a missing-feed window raises one silence incident scored `real_issue`; a late file is seen as late; a malformed results file gives a parser-failure incident; days without an injection raise nothing; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
