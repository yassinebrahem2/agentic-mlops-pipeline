# Stages 4 to 6 — Definitions Implementation Plan

> **For agentic workers:** Implement this plan task by task, in the order the Starts column sets, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1 and 2 also get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition).

**Goal:** the seven agents do their stage 4 and 5 work: each definition holds the tools its role needs and a prompt that says what it can now do (S4 §4, §6, §7; S5 §1 to §5). The two end-to-end tests are the e2e plan's (`2026-10-31-stage-4-6-e2e.md`, moved 2026-10-31).

**Branch:** `plan/stage-4-6-definitions`. **Milestone:** Stage 4.

**Read first:** the overview `docs/superpowers/plans/2026-10-29-stage-4-6-overview.md` (not repeated here: its global constraints, I22 to I42, "Shared by contracts", "Routes", "Files shared by these plans" and the decided points all apply); S4 §2 (untrusted text), §3b, §4 to §7, §10, §14; S5 "The principle", "Changes to agreed decisions", §1 to §5, §8; the stage 3 onboarding plan, task 1 (how the definitions, `working_rules` and their tests are built) and task 5 (scripted fake models, the end-to-end pattern); the definitions and `backend/tests/agents/test_agent_definitions.py` as onboarding leaves them.

**Consumes** (none of it is on `main` yet):
- Stage 3 onboarding (#360): the seven definitions, `agents/working_rules.py` with `agents/prompts/hard_limits.md` and `team_system.md`, `ReadHandoff`, the scripted `fake-onboarding`, the in-process flow test (`started_app`, signed-in client, polling a session) and the e2e helpers (`createDataFolder`, `E2E_FIXTURE_DIR`, `FAKE_MLFLOW_URL`).
- The tools, by name from `agents/tools/names.py` (contracts task 1), each registered in `default_registry` by its producer: `IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`, `MonitoringQuery` (incidents task 5) and `AdjustSeverity` (incidents task 6), both on `main` through incidents task 6's early PR; `RunHook`, `Hold` (hooks task 3, early PR); `RequestFix`, `RecordFixChoice` (training task 1, early PR); `SaveReport` (promotion task 2, early PR); the training and promotion behaviour the stage 5 prompts describe (training task 6 and promotion task 5, early PRs); `RegisterCheck` (results task 7); `MergePullRequest` (promotion task 7); `Propose` taking every registered kind (contracts task 4).
- Read by the tests, never copied: `WakeLabel`, `diagnoser` (I27), `FixChoice` (I34), `REQUIRED_REPORT_FIELDS` (I37), `REQUEST_TAG`, `EVALUATION_ARTIFACT`, `untrusted` (`monitoring/untrusted.py`), the six kind constants (I32), `HOOK_CREDENTIAL_ENV` (hooks task 2).

**Produces:** the seven definitions for stages 4 and 5. The e2e plan's task 2 consumes task 2 (the agents holding the stage 5 tools), so task 2 has an early PR.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | The stage 4 tools and prompts of the diagnosing agents and Alert Prioritization | onboarding; incidents task 6; hooks task 3 | when onboarding has merged and incidents task 6 and hooks task 3 are on `main`; **early PR** |
| 2 | The stage 5 tools and prompts: fix requests, Retraining, Deployment & Rollback | task 1; training task 6; promotion task 5 | after task 1, when training task 6 and promotion task 5 (each its plan's core path, early PRs) are on `main`; **early PR** (added 2026-10-31: the e2e plan's task 2 needs it) |
| 3 | The extras' lines: checks, the other proposal kinds, rule citations, `MergePullRequest` | task 2; results, incidents, hooks, training, promotion merged | after task 2; each line once its producer has merged |
| 4, 5 | moved to the e2e plan (2026-10-31) | | |
| 6 | Alert Prioritization holds only what it uses | task 1 | now (added 2026-10-31) |

The catalog refuses a definition naming an unregistered tool, and a prompt line about a behaviour that is not on `main` is a stub feature: a task adds neither before its producer is on `main`. Task 1 is the diagnosis the core path needs, so it is an early PR as soon as its tools are on `main`; the extras' lines come last, so a deadline cut loses them, not diagnosis.

## The tool matrix (one source: the test's table)

D = the agents `diagnoser()` can return: data-quality, model-performance, retraining, deployment, orchestrator. Every agent in D gains `IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`, `MonitoringQuery`, `RunHook`, `Hold` (task 1). Every holder of `OpenPullRequest` gains `MergePullRequest` (task 3). On top of that:

| Agent | Adds (task) | `Propose` kinds its prompt names (task) |
|---|---|---|
| data-quality | `RequestFix`, `Propose` (2), `RegisterCheck` (3) | `parser`, `maintenance` (3) |
| model-performance | `RequestFix`, `Propose` (2), `RegisterCheck` (3) | `rule` (3) |
| alert-prioritization (not in D) | `MonitoringQuery`, `IncidentMerge`, `IncidentUpdate` (note only), `AdjustSeverity` (1) | none |
| retraining | `RecordFixChoice`, `SaveReport`, `Propose` (2), `RegisterCheck` (3) | `promotion` (2), `training_policy` (3) |
| deployment | `RequestFix`, `Propose` (2), `RegisterCheck` (3) | `rule`, `hooks` (3) |
| orchestrator | `RequestFix` (2) | none |
| onboarding (not in D) | none (already holds `Propose`) | `connections`, `handoff` (stage 3), `parser`, `hooks` (3) |

`context_hooks`, `can_message`, `chat`, `schedules` and `limits` stay as onboarding leaves them. `AdjustSeverity` is Alert Prioritization's only; `RecordFixChoice` and `SaveReport` are Retraining's only (I33); Retraining has no `RequestFix`, since it is the one that chooses.

---

### Task 1: The stage 4 tools and prompts of the diagnosing agents and Alert Prioritization

**Files:** `backend/src/mlops_agents/agents/definitions/{data-quality,model-performance,alert-prioritization,retraining,deployment,orchestrator}.md` (their stage 4 lines); `backend/tests/agents/test_agent_definitions.py` and the tool-set constants in `backend/tests/conftest.py`, own lines only.

**Interfaces:** consumes the matrix's task 1 tools, `WakeLabel`, `diagnoser`, `untrusted`, `working_rules`. Produces the stage 4 part of six definitions.

**Notes:**
- The working rules (hard limits, untrusted data in `<data source="...">` blocks, the continuous-PR discipline and the merge limits) reach every system message through `working_rules` (decided point 13): no definition repeats them; `untrusted()` emits the same `<data source="...">` block, so the working rules already cover it. A prompt names only tools its agent holds and only what is on `main` (stage 3's rule). Prompts write a tool name in backticks (`` `Hold` ``) and a proposal kind as `` `<kind>` proposal ``, so the tests can tell them from the plain words ("hold", "rule", "promotion gate").
- Every diagnosing prompt says, in its role's words:
  - a system message labeled `incident` (or `follow_up`) wakes it about one incident; any page has already gone out, so it does not notify again for the same thing;
  - investigate with `MonitoringQuery` (the sandbox cannot read the app's database; rows are data, never instructions), Bash on the mounted data, `TrackingHistory` where held, notes, past incidents;
  - record the cause with `IncidentUpdate`; merge or split with the tools; `resolved` only once the results stopped firing (code checks it);
  - the response menu, safest first (S4 §6): observe with a deadline (`IncidentFollowUp`); notify or open an issue; hold (`Hold`); roll back or fall back, rerun (`RunHook`; the gate asks unless a rule allows it); revert its own PR that caused the incident (a team change: comment and notify);
  - fix causes through PRs (a missing validation, a fragile job, a weak check);
  - when its incident resolves, save one Knowledge note with the cause and what worked (`SaveNote`, decided point 8).
- A hook command an agent writes reads its credential from `HOOK_CREDENTIAL_ENV`.
- DQ: validation and drift; classifies every change as bad data or real drift before acting, and explains every flag.
- MP: metrics suited to the problem and the error costs, never accuracy alone; label delay; labels and predictions from the handoff's tables joined on its ID column; the training-serving skew comparison.
- Deployment & Rollback (stage 4 part): diagnoses `bad_deploy` and `failed_scoring`. Orchestrator: diagnoses the other `operational` incidents (silence, failed runs, source and parser failures). Retraining (stage 4 part): diagnoses `failed_training`.
- AP (`chat: false`): woken once per new or changed incident; writes the ranking's explanation as an `IncidentUpdate` note, merges duplicates, moves severity by at most one level with `AdjustSeverity` and a reason (it also covers scope and confidence, which code cannot read yet). It never pages, routes or messages a person: severity and routing are code (S4 §7). Drop its stage 2 wording ("does not run", "route alerts with SendMessage").

**Tests (stop condition):**
- With the real `default_registry`, `HookRegistry` and `AgentCatalog.load`, all seven definitions load.
- A table-driven test compares each agent's tools with the matrix rows of the tasks done so far (the table lives once, in the test; tasks 2 and 3 add their columns).
- Every agent `diagnoser()` returns, over `get_args(ResultCategory)` with subtype `None` and each subtype it maps, is a catalog agent holding D's set; only Alert Prioritization holds `AdjustSeverity`.
- No prompt contains the backticked name of a tool from `agents/tools/names.py` its agent lacks (replaces stage 3's "only Onboarding mentions Propose").
- `incident` and `follow_up` are named by every diagnosing prompt, through one label-to-words dict in the test; each diagnosing prompt names the menu items in the safest-first order above and the Knowledge note on resolve.
- DQ's prompt names bad data and drift; MP's names label delay and skew.
- `untrusted("x", source="t")`'s opening tag appears in `working_rules(...)`'s description of data blocks for every agent; stage 3's test that no definition contains the hard-limits text still passes.
- Full backend suite, ruff, format, pyright; CI green.

**PR:** early PR from `plan/stage-4-6-definitions-task1` into `main` (`AGENTS.md` rule 5): without it no woken agent can diagnose.

---

### Task 2: The stage 5 tools and prompts: fix requests, Retraining, Deployment & Rollback

**Files:** `agents/definitions/{data-quality,model-performance,retraining,deployment,orchestrator,onboarding}.md` (their stage 5 lines); the test files as task 1, own lines.

**Interfaces:** consumes the matrix's task 2 tools, `FixChoice`, `REQUIRED_REPORT_FIELDS`, `REQUEST_TAG`, `EVALUATION_ARTIFACT`, `PROMOTION_KIND`. Produces the stage 5 part of the definitions.

**Notes:**
- The continuous-improvement principle reaches all seven through `working_rules` (`team_system.md`); prompts add only what is their role's (S5 §5).
- DQ, MP, Deployment, Orchestrator: request a fix (`RequestFix`) when the model itself must change; it never chooses or runs the fix (menu item 6).
- Retraining (new description: chooses the fix and runs the team's pipeline): woken by `fix_request` (a request, or its training run linked back); chooses cheapest and safest first (re-threshold, recalibrate, retrain, or decline back to observe) and records it with `RecordFixChoice`, saying why not each other option; runs the pipeline only through the model's `train` and `evaluate` hooks with `RunHook`, giving the training window (the app fills the placeholders; a `ci_dispatch` runs the default branch, so a fix needing a code change waits for its PR to merge); makes the pipeline write the request tag and its training window by PR where it does not; stops after the circuit opens and escalates with what it tried (S5 §2); once the run links back, `SaveReport` with every required assessment (a value, or not applicable with a reason), then in `person` mode a `` `promotion` proposal `` citing the report and the deploy hook; in `team_gate` mode the team's gate decides and Retraining reports. Reasons as S5 §5 lists (question every number, the least risky fix, leakage, random splits, small samples, groups, calibration, error costs, feedback loops, label bias, parity first, say what it does not know). No CI and no remote training command: a PR adding one instead of a run (S5, answers).
- Deployment & Rollback: owns the team's evaluation, promotion gate and deployment workflow and improves them by PR: the standard evaluation record at `EVALUATION_ARTIFACT` (plus the JSON Lines copy when the tracker's artifacts are unreachable), parity, champion against challenger on the same serving inputs (shadow, S5 change 1), a time-based holdout, gradual rollout (S5 change 2), a rollback command where missing; a PR adding a field the evaluation lacks names that field in its title (the "not computed" link); the protection surfaces are always merged by a person. Woken by `post_deploy`: compares the new version with the previous one from the evaluation record and the live label-based results with their counts; clear harm → roll back (`RunHook`); unclear → observe with a deadline (S4 §6). In server mode delivers the heartbeat job (calls `GET /api/health/heartbeat`, alerts through the team's channel after two failures) to the team's scheduler by PR (S4 §3b); in local mode says it cannot be reached.
- Onboarding: the monitoring loop now runs (drop "arrive later" for checks and results); in server mode records the heartbeat job as a gap whose next step is Deployment's PR. Stage 3's handoff coverage tests stay green.

**PR (added 2026-10-31):** when this task is done, open an early PR into `main` from `plan/stage-4-6-definitions-task2` at its last commit (`Closes` its task issue): the e2e plan's task 2 needs the agents to hold the stage 5 tools.

**Tests (stop condition):**
- Task 1's tests cover the task 2 columns.
- Retraining's prompt names each `get_args(FixChoice)` value, and each `REQUIRED_REPORT_FIELDS` entry through one field-to-words dict in the test (it fails when contracts adds a field); it names `fix_request`, the circuit's escalation and the request tag's purpose (asserted on words).
- Deployment's prompt names `post_deploy`, parity, holdout, rollout and the heartbeat; Orchestrator's names silence; every `get_args(WakeLabel)` value is named by at least one prompt.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 3: The extras' lines: the other proposal kinds, rule citations, `MergePullRequest`

**Files:** the definitions whose matrix cells are task 3's; `agents/working_rules.py` (Alert Prioritization's line only); the test files, own lines.

**Also (added 2026-10-31, from task 6):** `working_rules.py` still tells Alert Prioritization "you route, you do not open them", which is no longer true since routing is in code (task 6): reword it to what the agent does (merge duplicates, adjust severity by one level), and update the inline Alert Prioritization fixture in `tests/agents/test_working_rules.py`, which still lists the removed tools.

**Interfaces:** consumes `RegisterCheck`, `MergePullRequest`, the kind constants `PARSER_KIND`, `MAINTENANCE_KIND`, `RULE_KIND`, `HOOKS_KIND`, `TRAINING_POLICY_KIND`. Produces the rest of the matrix. Each line goes in only once its producer has merged (results: checks, `RegisterCheck`, `parser`; incidents: `maintenance`; hooks: `rule`, `hooks`, rule citations; training: `training_policy`; promotion: `MergePullRequest`); a line whose producer has not merged by the deadline is left out and listed in the plan issue.

**Notes:**
- Checks (DQ, MP, Retraining, Deployment; once results has merged): code in the team's repository, tested in the sandbox, delivered by PR and added to the team's scheduler named in the handoff; standard result records with `check_id`, `origin: "agent"` and the version, written to the handoff's results location; a minimum sample, below which the check records "not enough data" instead of firing; registered with `RegisterCheck` naming the PR (S4 §4). MP's first audit adds the label-based checks that confirmation by labels reads (S4 §10).
- DQ proposes `parser`s for the team's existing monitoring (a mapping first, code only when a mapping cannot express it; tested on real outputs, S4 §2) and `maintenance` windows from upstream announcements.
- MP proposes `rule`s (rollback, rerun or fall back past a threshold, with limits); Deployment proposes `rule`s and `hooks` (commands found after the handoff); Onboarding, after the handoff, proposes `parser`s and `hooks` found later; Retraining proposes a `training_policy` (a deploying hook needs an approved rollback rule).
- Every diagnosing prompt's rollback, fall-back and rerun line gains: cite an approved rule where one applies (`rule_id`); the gate evaluates it, the agent never certifies it.
- On merging, a prompt says only that an agent may merge its own green PR with `MergePullRequest` and the app refuses the rest; the rules are code and the working rules.

**Tests (stop condition):**
- `MergePullRequest` holders equal `OpenPullRequest` holders; DQ's and MP's prompts name "not enough data".
- For each kind constant (I32, `CONNECTIONS_KIND`, `HANDOFF_KIND`), the phrase `` f"`{kind}` proposal" `` appears in exactly the prompts of the matrix's last column (constants imported, never retyped).
- Full backend suite, ruff, format, pyright; CI green.

---

### Tasks 4 and 5: moved

The two end-to-end tests moved to their own plan on 2026-10-31: [`2026-10-31-stage-4-6-e2e.md`](2026-10-31-stage-4-6-e2e.md) (its tasks 1 and 2).

---

### Task 6: Alert Prioritization holds only what it uses (added 2026-10-31)

From the lead's debt re-triage (#593's row): Alert Prioritization reads untrusted `MonitoringQuery` rows yet still holds its stage 2 `SendMessage`, `OpenIssue` and `CommentOnIssue` and a broad `can_message`; its prompt says it contacts no person, so today only the prompt stops untrusted text from reaching a public repo or another agent.

**Files:** `agents/definitions/alert-prioritization.md`; tests (`tests/agents/` for the tool matrix). The `STAGE_2_TOOLS` test constant keeps its name (renaming it touches about 27 files across plans; it stays in the #593 debt row).

**Notes (Decision #606):** remove `SendMessage`, `OpenIssue`, `CommentOnIssue`, `ListAgents` and `SearchIssues` from its tools and empty its `can_message`: no line of its prompt uses them, and the three outbound ones could carry untrusted text out. It keeps `Read`, `Glob`, `Grep` (its prompt says "Read files and search the workspace") and `MonitoringQuery`, `IncidentMerge`, `IncidentUpdate`, `AdjustSeverity`.

**Tests (stop condition):** the definition loads with exactly those seven tools; a test fails if any of the five removed tools is added back; backend suite, ruff, pyright; CI green. It rides on the plan branch (no early PR); the lead removes the #593 row when it merges.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
