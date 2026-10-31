# Stages 4 to 6 — Definitions Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; task 1 also gets an early PR (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition).

**Goal:** the seven agents do their stage 4 and 5 work: each definition holds the tools its role needs and a prompt that says what it can now do (S4 §4, §6, §7; S5 §1 to §5); two end-to-end tests prove the monitoring night (S4 §14, Done when) and the path from an incident to a fix and a promotion (S5 §8).

**Branch:** `plan/stage-4-6-definitions`. **Milestone:** Stage 4.

**Read first:** the overview `docs/superpowers/plans/2026-10-29-stage-4-6-overview.md` (not repeated here: its global constraints, I22 to I42, "Shared by contracts", "Routes", "Files shared by these plans" and the decided points all apply); S4 §2 (untrusted text), §3b, §4 to §7, §10, §14; S5 "The principle", "Changes to agreed decisions", §1 to §5, §8; the stage 3 onboarding plan, task 1 (how the definitions, `working_rules` and their tests are built) and task 5 (scripted fake models, the end-to-end pattern); the definitions and `backend/tests/agents/test_agent_definitions.py` as onboarding leaves them.

**Consumes** (none of it is on `main` yet):
- Stage 3 onboarding (#360): the seven definitions, `agents/working_rules.py` with `agents/prompts/hard_limits.md` and `team_system.md`, `ReadHandoff`, the scripted `fake-onboarding`, the in-process flow test (`started_app`, signed-in client, polling a session) and the e2e helpers (`createDataFolder`, `E2E_FIXTURE_DIR`, `FAKE_MLFLOW_URL`).
- The tools, by name from `agents/tools/names.py` (contracts task 1), each registered in `default_registry` by its producer: `IncidentUpdate`, `IncidentMerge`, `IncidentSplit`, `IncidentFollowUp`, `MonitoringQuery` (incidents task 5) and `AdjustSeverity` (incidents task 6), both on `main` through incidents task 6's early PR; `RunHook`, `Hold` (hooks task 3, early PR); `RequestFix`, `RecordFixChoice` (training task 1, early PR); `SaveReport` (promotion task 2, early PR); the training and promotion behaviour the stage 5 prompts describe (training task 6 and promotion task 5, early PRs); `RegisterCheck` (results task 7); `MergePullRequest` (promotion task 7); `Propose` taking every registered kind (contracts task 4).
- Read by the tests, never copied: `WakeLabel`, `diagnoser` (I27), `FixChoice` (I34), `REQUIRED_REPORT_FIELDS` (I37), `REQUEST_TAG`, `EVALUATION_ARTIFACT`, `untrusted` (`monitoring/untrusted.py`), the six kind constants (I32), `HOOK_CREDENTIAL_ENV` (hooks task 2).
- For the end-to-end tests: the services, routes, jobs and fakes of the plans named in each task's Starts (`Scheduler.run_due`, the `clock` parameter of `create_app`, the fake Discord and SMTP servers); stage 2's `FakeDockerRunner`, fake GitHub (`workflow_runs`, pulls) and fake OpenAI server (on `main`).

**Produces:** the seven definitions for stages 4 and 5; the scripted fake models of the two stories; `backend/tests/e2e/` and the two Playwright specs. No interface another plan consumes.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | The stage 4 tools and prompts of the diagnosing agents and Alert Prioritization | onboarding; incidents task 6; hooks task 3 | when onboarding has merged and incidents task 6 and hooks task 3 are on `main`; **early PR** |
| 2 | The stage 5 tools and prompts: fix requests, Retraining, Deployment & Rollback | task 1; training task 6; promotion task 5 | after task 1, when training task 6 and promotion task 5 (each its plan's core path, early PRs) are on `main` |
| 3 | The extras' lines: checks, the other proposal kinds, rule citations, `MergePullRequest` | task 2; results, incidents, hooks, training, promotion merged | after task 2; each line once its producer has merged |
| 4 | The stage 4 end-to-end test (the simulated night) | tasks 1 to 3; notifications, stats, ui merged | after task 3, when notifications, stats and ui have merged |
| 5 | The stage 5 end-to-end test (incident to fix to promotion) | task 4 | after task 4 |
| 6 | Alert Prioritization holds only what it uses | task 1 | now (added 2026-10-31) |

The catalog refuses a definition naming an unregistered tool, and a prompt line about a behaviour that is not on `main` is a stub feature: a task adds neither before its producer is on `main`. Task 1 is the diagnosis the core path needs, so it is an early PR as soon as its tools are on `main`; the extras' lines come last, so a deadline cut loses them, not diagnosis. If time runs short, the rule, escalation and morning-report steps of task 4 move into a later task and say so in the plan issue.

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

**Tests (stop condition):**
- Task 1's tests cover the task 2 columns.
- Retraining's prompt names each `get_args(FixChoice)` value, and each `REQUIRED_REPORT_FIELDS` entry through one field-to-words dict in the test (it fails when contracts adds a field); it names `fix_request`, the circuit's escalation and the request tag's purpose (asserted on words).
- Deployment's prompt names `post_deploy`, parity, holdout, rollout and the heartbeat; Orchestrator's names silence; every `get_args(WakeLabel)` value is named by at least one prompt.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 3: The extras' lines: the other proposal kinds, rule citations, `MergePullRequest`

**Files:** the definitions whose matrix cells are task 3's; the test files, own lines.

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

### Task 4: The stage 4 end-to-end test (the simulated night)

**Files:** new `backend/tests/e2e/__init__.py`, `backend/tests/e2e/fixture_project.py` (the fixture project: the handoff payload builder, with a Playwright variant that has only file and MLflow sources, and the results writer; one source for the backend tests and the scripted onboarding model), `backend/tests/e2e/harness.py` (the shared clock, the two-app start and restart on one data folder and key store, `run_due` at an instant, waiting for a woken turn; task 5 reuses it), `backend/tests/e2e/test_monitoring_night.py`, `backend/tests/fakes/monitoring_script.py` (the scripted replies of each woken agent and of `fake-onboarding-night`), a `MODELS` entry and a dispatch line per scripted model in `backend/tests/fakes/fake_openai.py`, placed before its generic "last message is a tool result" branch; `backend/tests/api/conftest.py` (`keystore` and `clock` parameters of `build_app` and `started_app`, so a restarted app keeps the Discord URL and the hook credentials); `frontend/e2e/monitoring.spec.ts`.

**Interfaces:** consumes the Starts plans through their services, routes and fakes (`create_app(clock=...)`, `Scheduler.run_due`, the fake Discord and SMTP servers, `Verdicts`, `Baseline`). Produces the test and the scripted models.

**Notes:**
- In process: `started_app` with `FakeDockerRunner`, the fake OpenAI, GitHub and Discord servers in process, and one fake `Clock` passed to `create_app`, so every stage 4 to 6 service shares it. The night is driven by setting the clock and calling `Scheduler.run_due(project_id, now)` at each instant; nothing sleeps. Wait for a woken agent's turn to end by polling its session, as onboarding's flow test does. The scripted models call tools in a fixed order and never depend on server state.
- The fixture: one model, a fixed project time zone and a night with no DST change, level `ask`. Pipeline stages `ingest_bookings` (outputs `bookings`), `ingest_weather` (outputs `weather`) and `score` (inputs both, outputs `predictions`, and listing the model as incidents task 4 reads "a stage listing a model", so `weather`'s silence is high), with edges into `score`: so `predictions` is downstream of `bookings`, and `weather` is not. A local-folder connection holds the team check's JSON Lines (a mapping parser) and the agent check's records; `weather` is a daily data feed with a deadline in the night, and the fixture sets `second_deadline_minutes` so its second deadline falls after the morning (it stays high). The model's production signal is a `ci_job`: the scoring workflow, whose runs come from the fake GitHub's `workflow_runs` through a GitHub Actions tracking source; a failed run is a `ci` record whose subject is the workflow (results task 4), which incidents classifies as `failed_scoring` (incidents task 3) and which matches no stage. Two on-call people (Discord, hours 09:00 to 18:00, an escalation order). A `rollback` command hook whose declared entry files exist in the fixture repository at the approved commit (the pin check runs on every sync), and a rule allowing it when the scoring workflow's latest run failed (its term's `check_id` is the `ci` record's check ID: the source ID `ci:<workflow>` unless a handoff check names the workflow). The parser and the rule exist before the night through the stage 3 proposals service and the approve route (no agent proposes them in this story).
- Every agent the story wakes has a scripted model, Alert Prioritization included (woken on each new or changed incident); so does the Playwright spec.
- Stopping halfway: close the first app's lifespan mid-night, write the records that arrive while it is off (a condition on table `holidays` that clears before the restart, one on table `rates` still firing; neither table is in a stage, so neither attaches to an open incident), start a second app on the same data folder with the same key store, and run the missed jobs.
- First check that `StartTask`, `Write` and `OpenPullRequest` work against the fake GitHub without Docker (task 5 needs them); if they do not, raise it in the plan issue rather than work around it.

**Tests (stop condition), one story:**
- The team check fires on `bookings`: stored, no incident, no notification; the incident on `bookings` later gets a baseline link to it.
- The agent's check fires on `bookings`: one `data_quality` incident; Data Quality is woken once with `incident` and records the cause; the same check fires on `predictions` (downstream) and attaches to it, with no second notification.
- The `weather` feed misses deadline plus grace: one silence signal in its own incident, high (production depends on it), not sent outside the person's hours, and in the morning report.
- The scoring workflow's run fails (production harmed now, subtype `failed_scoring`): its own critical incident; the fake Discord receives the page before the fake model receives Deployment & Rollback's first request; its `RunHook` citing the rule runs the rollback hook without an approval, and the timeline has `rule.satisfied` and `hook.ran`; unacknowledged past the timeout, the second person gets the escalation.
- After the restart: every record stored once; the cleared condition is one incident resolved while offline, waking no one; the still-firing one opens and notifies once; no incident, notification or wake is duplicated (counted by cause key, `notification.sent` events and `agent_wakes` rows).
- The morning report to each person lists the night's incidents, the rollback with its rule, what waits for a person and the resolved-offline incident.
- A person's verdict through the route is stored; a person's close then succeeds.
- `monitoring.spec.ts` (E2E CI has no Docker, no fake GitHub and no settable clock, so the spec covers what a person sees): onboard with `fake-onboarding-night` (the Playwright variant of the fixture handoff: no source that fails, and no feed or check whose deadline can fall inside the run, so no `source_failure` or silence incident reaches the Inbox); set the collection interval to its minimum (10 s) in project settings; append a fired agent record to the fixture folder; the Inbox lists the incident with its severity; the incident page shows the scripted cause on its timeline; a "Real issue" verdict is recorded; the Dashboard counts the open incident.
- Full backend suite, ruff, format, pyright; `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e` on Windows and Linux, no folder left behind; CI green, every job.

---

### Task 5: The stage 5 end-to-end test (incident to fix to promotion)

**Files:** new `backend/tests/e2e/test_fix_and_promotion.py`; `fixture_project.py` and `monitoring_script.py` (own additions); new `backend/tests/e2e/seeded_mlflow.py` (seeds a `FakeMlflowState` through the fake's existing functions and serves `create_fake_mlflow_app`); its `webServer` entry and port constant in `frontend/playwright.config.ts` and `frontend/e2e/env.ts` (own lines); `frontend/e2e/actions.spec.ts`.

**Interfaces:** consumes training's and promotion's services, routes and fake functions (the fake GitHub's workflow dispatch; the fake MLflow's tags, model versions and run artifacts). Adds no function to those fakes: it seeds through the ones they have.

**Notes:**
- Parametrised over the two promotion modes, one project each, both with a tracker-alias production signal. `person`: the `train` hook is a `ci_dispatch` with `deploys: false`, and the `deploy` hook a `command` (`FakeDockerRunner`). `team_gate`: the `train` hook declares `deploys: true`, so the moved alias is expected and the alias check suspends nothing. No training policy: the run asks, and the test approves it as the person.
- Uses task 4's `harness.py` (shared clock, `run_due`, waiting for turns): the tracking sync, linking and the post-deploy trigger run as jobs.
- Before the story, the fake MLflow holds the champion: a version under the production alias with its own `evaluation.json`. The test then plays the team's pipeline: after the dispatch it creates a run tagged `REQUEST_TAG` = the dispatched `request_id`, a model version and its `evaluation.json` with the champion's `evaluation_commit` and `gate_config_version` (so `comparable` holds), `champion_version` equal to the seeded champion, a run end inside `evaluation_max_age_hours` of the fake clock (so `validate` passes) and, in `team_gate`, the gate's `decision` and `reasons`; the fixture repository holds the train hook's workflow file and every declared entry file at the approved commit (the pin check); it moves the production alias only where the team's pipeline would (after the deploy hook; in `team_gate`, as the run's end), and in `person` mode only after the train run's alias check.

**Tests (stop condition):**
- A confirmed decay (MP's label-based check fires): Model Performance calls `RequestFix`; Retraining is woken with `fix_request`, records `retrain` with reasons for the other options, and its `RunHook` on the train hook asks; after the approval the guards pass and the fake GitHub receives one dispatch on the default branch with `{request_id}` filled.
- The tagged run links the request (`training.linked`); Retraining, woken again with `fix_request`, reads the evaluation and stores a complete report with `SaveReport`.
- `person`: `Propose` of a `promotion` creates a proposal; approving it re-validates and runs the deploy hook (basis `approval`, `promotion.applied`).
- `team_gate`: no deploy hook runs; the gate's decision is read from the evaluation record and the first on-call person is notified.
- Both: the moved alias wakes Deployment & Rollback once with `post_deploy`, whose scripted turn registers a follow-up.
- Deployment & Rollback, messaged by the test, opens a PR improving the evaluation step through `StartTask`, `Write` and `OpenPullRequest` against the fake GitHub.
- `actions.spec.ts` (with the seeded fake MLflow): a person-mode promotion proposal (a scripted Retraining turn) shows its card with the assessments, computed values marked as computed, and champion against challenger; Reject records the rejection; the Deployments page shows the environment's version.
- Full backend suite and checks as task 4; `npm run e2e`; CI green, every job.

---

### Task 6: Alert Prioritization holds only what it uses (added 2026-10-31)

From the lead's debt re-triage (#593's row): Alert Prioritization reads untrusted `MonitoringQuery` rows yet still holds its stage 2 `SendMessage`, `OpenIssue` and `CommentOnIssue` and a broad `can_message`; its prompt says it contacts no person, so today only the prompt stops untrusted text from reaching a public repo or another agent.

**Files:** `agents/definitions/alert-prioritization.md`; tests (`tests/agents/` for the tool matrix). The `STAGE_2_TOOLS` test constant keeps its name (renaming it touches about 27 files across plans; it stays in the #593 debt row).

**Notes (Decision #606):** remove `SendMessage`, `OpenIssue`, `CommentOnIssue`, `ListAgents` and `SearchIssues` from its tools and empty its `can_message`: no line of its prompt uses them, and the three outbound ones could carry untrusted text out. It keeps `Read`, `Glob`, `Grep` (its prompt says "Read files and search the workspace") and `MonitoringQuery`, `IncidentMerge`, `IncidentUpdate`, `AdjustSeverity`.

**Tests (stop condition):** the definition loads with exactly those seven tools; a test fails if any of the five removed tools is added back; backend suite, ruff, pyright; CI green. It rides on the plan branch (no early PR); the lead removes the #593 row when it merges.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
