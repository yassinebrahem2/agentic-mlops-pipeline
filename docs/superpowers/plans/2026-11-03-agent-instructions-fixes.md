# Agent Instructions and Tool Descriptions (walkthrough 3) Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One owner, one branch `plan/agent-instructions-fixes`, **one PR** for the whole plan (`AGENTS.md` rule 5), closing the plan issue and all five task issues: the tasks edit the same prompt files, so they go in order on one branch and through one CI cycle. Push each finished task to the branch; open the PR when task 5 is done. Paths are under `backend/src/mlops_agents/`. Every text stays generic: no project, dataset, company or demo names.

**Goal:** the agents' own instructions and tool descriptions caused much of what went wrong in the lead's walkthrough 3 (2026-11-03; fix list items 1, 2, 3, 4, 6, 9, 11, 12, 15, 19, 24, 31, 32): Onboarding asked 13 questions about things the repository answered, and then filled in values nobody gave (a made-up on-call address, an `echo` command, a threshold); it put `inputs` on plain commands and could not say "no on-call"; the audits spent their steps exploring, re-read the same files, wrote issues instead of checks, and claimed tests they never ran; agents opened PRs without running the team's CI; TrackingHistory's one-line description sent agents to reach MLflow from the sandbox; and the agents worked in silence.

**Branch:** `plan/agent-instructions-fixes`. **Milestone:** Stage 3: Onboarding and data access. **Consumes:** merged code; task 2's parser step reads the message the monitoring plan's task 3 sends (it may merge first: the instruction stands on its own). **Produces:** nothing other plans use.

**Review bar:** the lowered bar. **Stop condition for wording:** the definition and prompt tests below plus CI; the lead's walkthrough 4 checks the behaviour.

## Tasks

| Task | Title | Fix |
|---|---|---|
| 1 | Shared rules: speak up, run the team's CI, never claim what you didn't run | 11, 15, 24, 32 |
| 2 | Onboarding: find it yourself, ask only what people know, never invent | 1, 2, 19 |
| 3 | Handoff field descriptions | 3, 4, 6 |
| 4 | The audit delivers checks, in budget | 9, 31 |
| 5 | TrackingHistory's description | 12 |

---

### Task 1: Shared rules: speak up, run the team's CI, never claim what you didn't run

**Files:** `agents/prompts/hard_limits.md` (every agent); `agents/prompts/team_system.md` (agents that open PRs); `agents/working_rules.py` (`sandbox_limits`); `tests/agents/test_agent_definitions.py` or the working-rules tests.

**Notes (meaning; wording is the owner's, one short sentence each):**
- `hard_limits.md`: now and then, at the moments that matter (starting something new, a finding, before changing code), write one short line to the person saying what and why, then keep going in the same message; not a line per tool call. Don't re-read a file you already read in this turn unless it changed. Never say you ran or tested something you didn't.
- `team_system.md`: before opening or updating a PR, run what the team's CI runs (read `.github/workflows/`: lint, format, tests) in the sandbox and fix until it passes; if something can't run there, say which and why in the PR body.
- `sandbox_limits`: drop "The project's own test suite may not run there; report this as a gap rather than retried" (it taught agents to skip tests); keep "an environment failure is reported once".

**Tests:** the rendered rules for a PR-opening agent hold the CI rule; every agent's hold the speak-up and never-claim rules; the dropped sentence is gone.

### Task 2: Onboarding: find it yourself, ask only what people know, never invent

**Files:** `agents/definitions/onboarding.md`; its definition tests.

**Notes:**
- Interview: work out everything the repository, its config, its CI and (after connection approval) the data show; ask the person only what lives in people's heads (the objective in their words, costs, contacts, policy, where data and results live outside the repository). Give your best reading with each question; ask first what blocks the next step (the data location for `ProposeConnections`); a few questions at a time.
- Never fill in a value nobody gave and nothing shows (a threshold, a cost, a schedule time, a contact, a command): leave it out and record a gap.
- After the handoff is approved, without being asked: propose a parser for every results source the app lists as waiting for one (the app sends that list), each with the source id exactly as written; check with `MonitoringQuery` that results arrive; tell the person what the Dashboard now shows.
- Reword "Act instead of announcing the next step…" to keep its intent (never stop after only announcing) without forbidding the speak-up rule of task 1.

**Tests:** the definition holds the ask-only, never-invent and after-approval parser rules; no project names (the existing generic-text test, if any, covers it).

### Task 3: Handoff field descriptions

**Files:** `proposals/payloads.py` (`Field` descriptions only; no model or validator changes); tests that assert the schema text.

**Notes:**
- `CommandSpec.inputs`: only for a `ci_dispatch`; a command template takes no inputs.
- `operations.on_call`: when the team names no on-call people, pass an empty list and record a gap; never invent a contact.
- `data_roles`: one table may have more than one role (a table of served inputs that is also trained on gets a `training` and a `serving_inputs` row).
- Placeholders: the team's own names (`{date}`, `{version}`, `{cutoff}`) map to the declared ones (`window_start`/`window_end`, `candidate_version`).
- `label_maturity_days`: days after the event until a label is final (not zero when labels can still change).
- `results_location`: where the agents' own checks write; the team's existing checks are read through their own `results`.
- `feeds`: a `data` feed for a table that arrives on a cadence; a `job` feed for a job's result lines.
- `PipelineStage.inputs`/`outputs`: use the data roles' table names.
- Each added sentence counts against the onboarding schema cost (known-debt row on #734): keep each to one short sentence.

**Tests:** the generated JSON schema holds each sentence; the payload models are unchanged (existing tests pass).

### Task 4: The audit delivers checks, in budget

**Files:** `onboarding/briefs.py` (`_HOW_TO_AUDIT` and `AUDIT_BRIEFS`); `agents/definitions/data-quality.md`, `model-performance.md`, `retraining.md`, `deployment.md` (line 11 `max_steps`, and one line each); tests.

**Notes:**
- Audit brief: save a note after each finding, not at the end; a missing check is code you deliver (written, tested on the mounted data, the team's CI green, by PR, registered with `RegisterCheck` naming that PR); open an issue only for what you can't fix in code; check open issues before opening one; end with the report.
- `max_steps` 60 for the four audit agents (walkthrough 3 measured: Data Quality and Deployment needed three turns of 25 each, much of it re-reading and failed sandbox calls that the other walkthrough-3 plans fix).
- Deployment: propose an automatic rollback only for a model problem and only when an earlier version exists; never for drift in the input data. Propose a hook only for a command already on the team's default branch, never one an open PR adds.

**Tests:** the four definitions have `max_steps: 60`; the brief holds the deliver-by-PR and notes-as-you-go rules.

### Task 5: TrackingHistory's description

**Files:** `agents/tools/tracking_history.py`; its tests.

**Notes:**
- Description: it reads the app's own copy of the project's trackers (MLflow, CI), synced by the app, so there is no need to reach them from the sandbox; `metrics` needs a `run_key` (list `runs` first); which filters apply to which `what`; "No X recorded yet" means none recorded, and any source errors are listed after it.
- Replace `except Exception: source_errors = []` (~182) with a logged warning and a line in the output saying the sources could not be listed.

**Tests:** the description holds the run_key rule; a failing `sources()` call is logged and said in the output.

## Known debt added

None planned.
