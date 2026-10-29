# Stage 5 — Promotion Implementation Plan

> **Draft.** Missing: the lead's last review of the stage 4-6 plan set after the consistency pass.

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1, 2 and 5 get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition).

**Goal:** the app reads the team's evaluation record, keeps the agent's report on each candidate, and promotes the way the team decided: in `person` mode a `promotion` proposal whose approval re-validates in code and then runs the team's deploy hook; in `team_gate` mode it never deploys and reports the gate's decision. A moved production alias wakes Deployment & Rollback for the post-deploy comparison. The Deployments page gets its data, and agents merge their own PRs under the merge rules in code (S5 §3, §4, §6, "Hard limits and merging"; S4 §6 post-deploy comparison).

**Branch:** `plan/stage-5-promotion`. **Milestone:** Stage 5.

**Read first:** the [stages 4-6 overview](../2026-10-29-stage-4-6-overview.md) (do not repeat it here): Global constraints, I24, I27, I29, I30, I32, I33, I36, I37, I38, I40, "Routes", "Files shared by these plans", decided points 5, 9, 11, 16. Then S5 §3, §4, §6 and "Hard limits and merging" (decided point 5 replaces its "branch protection API"), and the roadmap's Permission risk classes and Hard limits.

**Consumes:**
- On `main`: I16 `Tracking.query` (`what` = `models`, `versions`, `runs`, `deployments`, `evaluations`), `EvaluationRecord`, `DeploymentRow`, `RunRow`, `ModelSummary.production_version`, `tracking/handoff_alias.py` (`resolve_candidate_aliases_fn`, the one production-alias rule); I20 `Handoffs.current` (`ModelSpec.promotion`, `environments`, `production_signal`); I19 `ProposalKind`, `ProposalKinds`, `ProposalsService.get_by_approval`; stage 2 `Tasks` (`open_tasks`, `repo_ref`, `TaskView.pr_number`, `review_kind`), the GitHub client (`integrations/github/api.py` `request`, `pulls.py`), `GitHubAuth.token`, `EventLog`, `PrSyncObserver`, `project_settings`; frontend `ApprovalControls`, `ProposalShell`.
- contracts task 1: I37 types and `REQUIRED_REPORT_FIELDS`, I38 and I40 types (`promotion/contract.py`), `MonitoringSettings.evaluation_max_age_hours`, the `rep` and `prm` prefixes, the event types `report.saved`, `promotion.applied`, `promotion.refused`, `pr.merged_by_agent`, the tool names, `untrusted()`, `REQUEST_TAG`, `PrObserver` and `PrSyncObserver.add_observer`/`sync_now`, `list_pull_files` and `PullRequest.changed_files`, `QueryProviders`, `Message`, `OnCallPerson`. contracts task 2: the `reports` and `promotions` tables. contracts task 3: `ActionCall`, `Authority`, `action_call`, the `merge_pr` cells, `PROPOSE_BYPASS_OVERRIDES`. contracts task 4: `PromotionPayload` and `PROMOTION_KIND` in the widened `ProposalView`, `ProposalCheck`, `Propose` taking every registered kind and applying an allowed propose at once.
- hooks task 1: `Hooks.get`, `Hooks.list`, `Hooks.runs`, `HookCall`; hooks task 3: `Hooks.run` as the tools use it (its command runner). training task 3: the lifecycle that fills `{candidate_version}`, `{champion_version}`, `{model}` for a deploy hook from `HookCall.params`. results tasks 1 and 3: `Scheduler.add_sync_observer`, `SyncObserver`. incidents task 2: `AgentWaker.wake` (label `post_deploy`). notifications tasks 1 and 2: `Notifier.notify`, `OnCallPeople.list` (task 5). Stage 3 knowledge (#406): `Knowledge.save` (task 7).

**Produces:**
- `promotion/evaluation.py`: `SqliteEvaluationReader` (`EvaluationReader`, I37) and `comparable(a, b) -> bool` (task 1, early PR).
- `promotion/reports.py`: `SqliteReports` (`Reports`); the tool `SaveReport(draft: ReportDraft)` (risk `read`); `GET /api/projects/{id}/reports/{report_id}` → `ReportView` (task 2, early PR).
- `promotion/promotions.py`: `SqlitePromotions` (`Promotions`); `promotion/kind.py`: `PromotionKind` (`ProposalKind` and `ProposalCheck`); the card `PromotionProposalCard` (task 3).
- `promotion/post_deploy.py`: the post-deploy `SyncObserver` (task 4); `promotion/team_gate.py`: the `team_gate` `SyncObserver` (task 5).
- `GET /api/projects/{id}/deployments` → `DeploymentsView` (I40); the `reports` and `promotions` query providers (task 6).
- `promotion/surfaces.py`: `ProtectionSurfaces` (I38); `integrations/github/merge.py`; the tool `MergePullRequest(pr_number: int)` (risk `deploy`); a `PrObserver` for PRs closed unmerged (task 7).
No type here is redefined: every I37, I38 and I40 type is contracts'.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | `EvaluationReader`, `comparable`, "not computed" | contracts tasks 1, 2; I16 | after contracts tasks 1 and 2; early PR |
| 2 | `Reports`, required fields, `SaveReport`, the report route | task 1; contracts tasks 1, 2 | after task 1; early PR |
| 3 | The `promotion` kind and card, `person` mode, re-validation, the deploy hook as apply | tasks 1, 2; contracts tasks 3, 4; hooks task 3; training task 3 | after task 2, hooks task 3, training task 3 and contracts task 4 |
| 4 | The post-deploy trigger | results tasks 1, 3; incidents task 2 | after task 3, results task 3 and incidents task 2 |
| 5 | `team_gate` mode | tasks 2, 3; results task 3; notifications tasks 1, 2 | after task 4 and notifications task 2; early PR |
| 6 | `GET .../deployments` (I40) and the query providers | tasks 2, 3, 5; hooks task 1 | after task 5 |
| 7 | `MergePullRequest`, its checks, `ProtectionSurfaces`, the closed-unmerged observer, merge events | contracts tasks 1, 3; hooks task 1; stage 3 knowledge (#406) | after task 6 and #406 merged |

Core path: tasks 1 to 5 (promote). Extras: task 6 (Deployments data) and task 7 (agents merging). Shared files: this plan adds only its own lines to `api/services.py` (`build_services`), `agents/tools/__init__.py` (`default_registry`) and `frontend/src/pages/agents/ApprovalCard.tsx`; its routes are in its own `api/routers/promotion.py`, found by `include_routers`. Every change to the API commits the regenerated `frontend/openapi.json`.

---

### Task 1: `EvaluationReader`, `comparable`, "not computed"

**Files:** new `backend/src/mlops_agents/promotion/evaluation.py`; `api/services.py` (construct it); tests `backend/tests/promotion/test_evaluation.py`.

**Interfaces:** consumes `EvaluationRecord`, `Tracking.query(what="evaluations", model=, version=)`, `Tasks.open_tasks`, `EvaluationPayload`, `EvaluationView`, `EvaluationReader` (I37). Produces `SqliteEvaluationReader(tracking, tasks)` and `comparable` (I37 gives it to this task).

**Notes:**
- Reads stage 3's `evaluations` only through `Tracking.query`: the tracker sync and the results copy (results task 4) both land there, so the record is read once whatever wrote it. Several sources: the newest `fetched_at` wins.
- The payload is the team's data: validated with `EvaluationPayload` (`extra="allow"`, `schema_version` 1). A payload that fails validation or has another `schema_version` gives `None` (logged once per record, no payload text in the log); never a partial view.
- `not_computed` is derived from `EvaluationPayload`'s own optional fields that are `None` in the record (iterate the model's fields; no second list of names). The value is the number of an open Deployment & Rollback task PR whose title names that field (the definitions plan tells the agent to name it so), else `None`.
- `comparable`: same `evaluation_commit` and `gate_config_version`, nothing else.

**Tests (stop condition)** (a fake in-test `Tracking` and `Tasks`): a record maps to its view; two sources pick the newest; a payload without `schema_version` 1 or with a bad field gives `None`; extra team fields survive; `not_computed` lists exactly the `None` optional fields, with the PR whose title names one; `latest` picks the newest; `comparable` true and false on each of the two fields; full backend suite, ruff, format, pyright; CI green.

**PR:** its own early PR into `main` from `plan/stage-5-promotion-task1` (`Closes #<task issue>`), per `AGENTS.md` rule 5.

---

### Task 2: `Reports`, required fields, `SaveReport`, the report route

**Files:** new `promotion/reports.py`, `agents/tools/save_report.py`, `api/routers/promotion.py` (the report route); `agents/tools/__init__.py` and `api/services.py` (own lines); tests `backend/tests/promotion/test_reports.py`, `backend/tests/agents/tools/test_save_report.py`; regenerated `frontend/openapi.json`.

**Interfaces:** consumes task 1, `ReportDraft`, `ReportField`, `ReportView`, `Reports`, `REQUIRED_REPORT_FIELDS`, the `reports` table, `new_id("rep")`, `EventLog`, the tool name. Produces `SqliteReports(databases, reader: EvaluationReader, events)`, the tool, and the route.

**Notes:**
- `save` refuses (`DomainError`, 422, naming every missing key) a draft missing any `REQUIRED_REPORT_FIELDS` key; "exactly one of value and reason" is `ReportField`'s own validator (contracts), not checked again. It records `report.saved` (subject: the report ID).
- `ReportView.evaluation` is read through the reader on every `get` and `for_candidate`, never stored with the report (one copy of the record).
- `for_candidate` returns the newest report for that model and version.
- The tool's output (report ID, the evaluation's decision, reasons and "not computed" fields) passes every value read from the evaluation record through `untrusted(..., source="evaluation")`: it is the team's text reaching an agent. `action_call` is `None`.
- The route answers 404 `report_not_found` for an unknown ID.

**Tests (stop condition):** a complete draft is stored and read back with its evaluation; each missing required field is refused, all missing keys named; `report.saved` recorded once; `for_candidate` returns the newest; an evaluation stored after the report shows on the next read; the tool refuses an incomplete draft as a `ToolFailure`, wraps the evaluation's text in the untrusted block, is `read` and allowed at every level; the route's 200 and 404; `openapi_export --check`; full suite and checks; CI green.

**PR:** its own early PR into `main` from `plan/stage-5-promotion-task2` (`Closes #<task issue>`): definitions task 2 gives Retraining `SaveReport`.

---

### Task 3: The `promotion` kind and card, `person` mode, re-validation, the deploy hook as apply

**Files:** new `promotion/promotions.py`, `promotion/kind.py`; `api/services.py` (construct, `proposal_kinds.register`, `approval_store.observers.add` for the Reject); new `frontend/src/pages/agents/proposals/PromotionProposalCard.tsx` and its test; `frontend/src/pages/agents/ApprovalCard.tsx` (one `CARD_FOR_PROPOSAL` line); tests `backend/tests/promotion/test_promotions.py`, `test_promotion_kind.py`.

**Interfaces:** consumes tasks 1, 2; `PromotionPayload`, `PromotionView`, `Promotions`, `PROMOTION_KIND`, `ProposalCheck`, the `promotions` table, `new_id("prm")`; `Handoffs.current`; `Tracking.query(what="models"|"runs")`; `MonitoringSettings.evaluation_max_age_hours`; `Hooks.get`, `Hooks.run`, `HookCall`, `Authority`. Produces `SqlitePromotions`, `PromotionKind`, the card.

**Notes:**
- **`validate` is the one place of the code checks** (propose, approval, `bypass`); each failure is one user-facing line: the report exists and is for this model and candidate; the model's current handoff says `promotion: "person"`; the deploy hook exists, is of kind `deploy`, belongs to this model and is `active` (the payload's hook ID is the agent's claim: a hook of any other kind would run the wrong command with basis `approval`); the candidate has an evaluation; it is fresh (the end of the record's `run_key` run, `RunRow.end`, within `evaluation_max_age_hours`; no end known: not fresh); the payload's `champion_version` equals the current production version (`ModelSummary.production_version`) and the record's `champion_version`; when there is a champion, its own evaluation record exists and is `comparable` with the candidate's (S5 §3: otherwise the champion is evaluated again first).
- **Kind:** `key(payload) = payload.model` (a newer proposal for the model supersedes the pending one); `credentials_needed` is `[]`; `check` (`ProposalCheck`) returns `validate`'s lines, so `Propose` refuses a doomed promotion before asking anyone. `warnings` states that approving runs the deploy hook.
- **Statuses:** `refused` = a code check or a failed deploy run refused it (reasons in `refusals`, event `promotion.refused`); `rejected` = a person's Reject, recorded by an `ApprovalObserver` (`decided`, `proposal_kind == PROMOTION_KIND`, status `rejected` only: Stop stores `cancelled` and expiry `expired`; the proposal through `ProposalsService.get_by_approval`), keyed by proposal; no event type exists for it (the approval's own event records it).
- **Apply** (idempotent; the proposals service re-applies an approved proposal with no result at startup): look up the promotion by `proposal_id` (unique); a finished one returns its stored outcome, an unfinished one (still `proposed`: a crash during the run) is recorded `refused` "interrupted; check the Deployments page"; neither runs the hook again. Otherwise write the row as `proposed` first, then run `validate`: refusals → `refused`, `promotion.refused`, `ApplyResult.failed`. Else run `Hooks.run(HookCall(hook_id=deploy_hook_id, reason=..., params={"candidate_version": ...}), Authority(basis="approval", ref=proposal_id, ...))` (not gated again, I30); an `ok` result → `applied`, `applied_at`, `promotion.applied`; a failed run → `refused` with the hook's detail. `decided_by`: `person`, or `agent_bypass` when the proposal has no approval (contracts task 4 applies an allowed propose at once).
- **Card:** the report (each assessment, or "not applicable" with its reason), champion against challenger (overall and per group, with intervals and `n`), computed values marked as computed by the team's step, "not computed" fields named with their PR, the evaluation's commit and gate version; Approve and Reject through `ApprovalControls`. Types only from `schema.d.ts`; the report through the task 2 route.

**Tests (stop condition)** (fake in-test `Tracking`, `Handoffs`, `Hooks`): `validate` returns each refusal above on its own and none on a good payload; `check` refuses at propose; apply with a good payload runs the deploy hook once with basis `approval` and the candidate version, and records `applied`; a champion that moved between propose and approval, and a stale evaluation, refuse without running the hook; a failed hook run records `refused`; applying the same proposal twice (and a recovery re-apply) runs the hook once; a `team_gate` model refuses; a hook of kind `train` cited as the deploy hook is refused; a second promotion proposal for the same model supersedes the first; a person's Reject records `rejected` once, and a Stop (`cancelled`) records nothing; an apply with no approval records `agent_bypass`. Card (vitest): renders report, comparison, computed and "not computed" marks; Approve and Reject call the approval route. Full suites and checks; CI green.

---

### Task 4: The post-deploy trigger

**Files:** new `promotion/post_deploy.py`; `api/services.py` (`scheduler.add_sync_observer`); tests `backend/tests/promotion/test_post_deploy.py`.

**Interfaces:** consumes `SyncObserver`, `Scheduler.add_sync_observer`, `AgentWaker.wake`, `Tracking.query(what="deployments", since=)`, `resolve_candidate_aliases_fn`, `Promotions.history`, `untrusted()`. Produces `PostDeployObserver`.

**Notes:**
- On each `synced`, the new `DeploymentRow`s whose alias is a production alias of the model (`resolve_candidate_aliases_fn`, the rule tracking uses; never a second one) and whose `to_version` is set wake `deployment` with label `post_deploy`, `subject_id` the promotion ID when a promotion of that version exists, else the model, and `key` the row's model, alias, versions and `seen_at`. The agent name is the definition's; take it from where incidents task 2 names the diagnosers, never a new literal.
- The text names the model, the old and new versions (through `untrusted()`: they are the tracker's values) and says to compare the evaluation record with the live label-based results (I37).
- Cursor: a `project_settings` key `promotion_deploy_cursor` (the last `seen_at` handled), set to now on a project's first run, so history never wakes anyone.

**Tests (stop condition)** (fake `Tracking`, `Handoffs`, recording `AgentWaker`): an alias moved to a production alias wakes once with the versions; a non-production alias and a removed alias wake no one; a handoff mapping `live` to production is honoured; a second `synced` with nothing new wakes no one; the first run after startup does not wake for older moves; full suite and checks; CI green.

---

### Task 5: `team_gate` mode

**Files:** new `promotion/team_gate.py`; `promotion/promotions.py` (the `team_gate` record); `api/services.py`; tests `backend/tests/promotion/test_team_gate.py`.

**Interfaces:** consumes tasks 1 to 3, `SyncObserver`, `Tracking.query(what="versions"|"runs")`, `REQUEST_TAG`, `Notifier.notify`, `OnCallPeople.list`, `Message`. Produces `TeamGateObserver`.

**Notes:**
- The app never deploys here: no hook call exists in this module.
- On `synced`, for each model whose handoff says `team_gate`, each new candidate version whose tracker run carries `REQUEST_TAG` (a run the app requested; the team's own runs are the team's) and whose evaluation has a `decision` gets one promotion row (unique by model and candidate version): `mode="team_gate"`, `decided_by="team_gate"`, `promote` → `applied`, `hold` or `reject` → `refused` with the gate's reasons in `refusals` (the gate refused it; `rejected` is a person's); `report_id` from `Reports.for_candidate` (decided point 11: the app's report is a `rep` record). A record with no decision waits for the next sync. Events `promotion.applied` or `promotion.refused`. Cursor: a `project_settings` key `promotion_gate_cursor`, set to now on the first run.
- The first on-call person by `escalation_order` is told the result: `Notifier.notify(kind="promotion", incident_id=None)`, severity `low`, not urgent; the gate's reasons are the team's text, sent to the person as data.
- Stage 4 watches the new version through task 4's trigger when the alias moves; nothing else to do here.

**Tests (stop condition):** a `promote` decision on a requested run records one `applied` promotion and one notification; `reject` and `hold` record `refused` with the reasons; a run without the tag records nothing; a second sync adds nothing; a `person` model is ignored; no hook is ever run (a `Hooks` double that fails on any call); full suite and checks; CI green.

**PR:** early PR from `plan/stage-5-promotion-task5` into `main`: the end of the core path, which definitions task 2 describes to Retraining and Deployment & Rollback.

---

### Task 6: `GET .../deployments` (I40) and the query providers

**Files:** new `promotion/deployments.py`; `api/routers/promotion.py` (the route); `integrations/github/merge.py` (the open-PR labels read); `tests/fakes/fake_github.py` (its own lines: labels on the open pulls list); `api/services.py` (the two providers); tests `backend/tests/promotion/test_deployments.py`; regenerated `frontend/openapi.json`.

**Interfaces:** consumes `Tracking.query(what="versions"|"deployments")`, `Handoffs.current`, `Promotions.history`, `Hooks.runs`, `Tasks.open_tasks`, `QueryProviders`. Produces the route → `DeploymentsView`, and providers for `what="reports"` and `"promotions"`.

**Notes:**
- `environments`: one row per model and alias in `ModelSpec.environments`, with the version holding that alias now (an alias held by no version: `version=None`).
- `decisions`, newest first: applied promotions (`decided_by` from the record), and `Hooks.runs(kinds=["rollback", "fall_back"])` that succeeded: basis `rule` → `decided_by="rule"`, ref the rule ID; basis `approval` → `person`; basis `none` (allowed at `bypass`) → `agent_bypass`. Tracking's deployments give the versions and times; an alias move with no app record shows in `environments` only.
- `open_prs`: open agent task PRs (`TaskView.pr_number` set, `review_kind="github"`); labels read in one call (`GET /repos/{repo}/pulls?state=open`, in `integrations/github/merge.py`); GitHub unreachable → labels `[]`, the rest still answers.
- Providers render through `MonitoringQuery`'s `untrusted()` path; nothing new is stored.

**Tests (stop condition):** environments from a two-alias handoff; decisions from a promotion, a rule rollback, an approved fall-back and a `team_gate` record in order, with the right `decided_by`; open PRs with labels from the fake GitHub, and without when it fails; both providers page with `limit` and `offset`; `openapi_export --check`; full suite and checks; CI green.

---

### Task 7: `MergePullRequest`, its checks, `ProtectionSurfaces`, the closed-unmerged observer, merge events

**Files:** new `promotion/surfaces.py`, `promotion/pr_closed.py`, `integrations/github/merge.py` (its merge and check-run calls), `agents/tools/merge.py`; `agents/tools/__init__.py`, `api/services.py` (own lines; `pr_sync.add_observer`); `backend/tests/fakes/fake_github.py` (own routes only: check runs, `mergeable`/`mergeable_state`/`head.sha` on a PR; and the `sha` check of stage 2's merge route: 409 when the head moved); tests `backend/tests/integrations/test_github_merge.py`, `backend/tests/agents/tools/test_merge_pull_request.py`, `backend/tests/promotion/test_surfaces.py`, `test_pr_closed.py`.

**Interfaces:** consumes `ProtectionSurfaces`, `MergeCheck`, `ActionCall` (`kind="merge_pr"`), `PrObserver`, `PrChange`, `PrSyncObserver.sync_now`, `list_pull_files`, `PullRequest.changed_files`, `Hooks.list`, `Tasks`, `GitHubAuth.token`, `Knowledge.save`. Produces the tool, `GithubProtectionSurfaces`, `ClosedUnmergedObserver`.

**Notes (hard limits; all in code, at every level):**
- **Surfaces** (decided point 5): the `entry_files` of every model's `evaluate` and `deploy` hooks (`Hooks.list`, so proposal-added hooks count), every path under `.github/workflows/`, and `CODEOWNERS` in GitHub's three places (root, `.github/`, `docs/`). `touches` reads the PR's files with `list_pull_files` (every page, a rename's old and new path) and refuses when it returns fewer files than the PR's `changed_files` (a list it cannot see whole is never trusted). The agent's labels are never read.
- **Checks**, in `run` (after the gate; preview runs them too so a doomed merge is not asked): the PR is the calling agent's own (an open task of `ctx.agent` with that `pr_number`, `review_kind="github"`; a local review never self-merges); GitHub reports `mergeable: true` and `mergeable_state: "clean"`; the head commit has at least one passing check run and none failed or pending; no surface touched. Any failure → `ToolFailure` with every refusal.
- **Merge** with `PUT .../pulls/{n}/merge` and `sha` = the head commit the checks read, so a push after the checks fails the merge instead of merging unchecked code; never an admin flag. Then record `pr.merged_by_agent` (subject: the task ID) and call `sync_now` so the task ends `merged` and the `PrObserver`s run (pins: training).
- `action_call` returns `ActionCall(kind="merge_pr", ...)`; the cells are contracts' (deny at `suggest`, ask at `ask` and `autonomous`, allow at `bypass`).
- **Closed unmerged** (decided point 9; roadmap: "a PR closed unmerged becomes a Knowledge note with its reason"): on `PrChange(state="closed")`, one note per PR (idempotent by the note ID derived from the PR number): type `decision`, author `system`, title "PR #n closed unmerged: <title>", text the PR's last comment inside `untrusted()`.

**Tests (stop condition)** (the fake GitHub in-process, fake `Hooks`, `Tasks`, `Knowledge`): merges a clean own PR with a passing check, sends the head `sha`, records the event and calls `sync_now`; a moved head gets 409 and nothing is recorded; refuses another agent's PR, a local-review task, `mergeable_state` `blocked` or `dirty`, no check runs, a failed and a pending check, a PR touching `.github/workflows/ci.yml`, `CODEOWNERS`, `docs/CODEOWNERS`, a deploy hook's entry file, or renaming one away; a PR whose files exceed what GitHub returns is refused; denied at `suggest`, asked at `ask` and `autonomous`, allowed at `bypass` through the real gate; a closed-unmerged PR writes one note, twice-seen writes it once, a merged PR writes none; full suite and checks; CI green. No early PR: this is the last task.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
