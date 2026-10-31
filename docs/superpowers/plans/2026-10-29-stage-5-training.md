# Stage 5 — Training Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1, 2, 3 and 6 get early PRs (`AGENTS.md` rule 5). Each task lists its files, interfaces, the notes that matter and its tests (the stop condition).

**Goal:** a diagnosing agent requests a model fix, Retraining chooses it and runs the team's own pipeline through a parameterised hook, guarded in code and linked back to the tracker by a request ID (S5 §1, §2); then the extras: the circuit breaker, pins and the alias-history check that verify `deploys`, `GateFacts`, and training policies.

**Branch:** `plan/stage-5-training`. **Overview:** [`2026-10-29-stage-4-6-overview.md`](../2026-10-29-stage-4-6-overview.md): read it first (global constraints, I22 to I42, "Routes", "Files shared by these plans"); this plan does not repeat it. Specs: S5 §1, §2 (and the hard limits); S4 §6 (menu item 6).

**Tables** are contracts' (`fix_requests`, `training_requests`, `training_policies`, `hook_pins`, `hook_runs`, in contracts' project migration). This plan writes no migration; a missing column is a Decision issue.

**Consumes** (none of it is on `main` yet unless marked merged):
- contracts task 1: I34, I35, I36 types (`training/contract.py`, `monitoring/hooks/contract.py`, `RunContext.request_id: str | None`, `HookPinner`), the tool names, the `fix`, `trq`, `pol` prefixes, the stage 5 event types, `REQUEST_TAG`, `MonitoringSettings` (`circuit_breaker_fixes`, `circuit_breaker_window_days`), `untrusted()`, `PrObserver` and `PrSyncObserver.add_observer`, `QueryProviders`, `Message`, `OnCallPerson`; task 2: the tables; task 3: `ActionCall`, `Authority`, `ActionAuthority`, `AuthorityRegistry` (`register`, `set_facts`), `GateFacts` and its default; task 4: `TRAINING_POLICY_KIND`, `TrainingPolicyPayload` in the widened payload union.
- incidents task 1: `Incidents` (`get`, `list`, `signals`, `holds`, `raise_signal`, `set_severity`, `add_observer`), `IncidentObserver`; task 2: `AgentWaker`, `diagnoser`.
- hooks task 1: `Hooks` (`add_runner`, `add_lifecycle`, `set_pinner`, `get`, `list`, `suspend`, `set_deploys_verified`, `approve`), `HookView`, `HookCall`, `HookResult`, `HookRefused`; task 2: `fill_template`; task 5: `Rules.list` (task 8 only).
- results task 3: `SyncObserver`, `Scheduler.add_sync_observer`; task 5: `Expectations` (`register`, `retire`, `states`).
- notifications tasks 1 and 2: `Notifier`, `OnCallPeople` (task 6 only).
- Merged: `Tracking.query` and the row types (I16), `Handoffs.current` and `ModelSpec`, `Profile.label_maturity_days`, `CommandSpec`, `DataRole` (I19, I20), `WorkspaceService`, `GitRunner`, `github_remote`, `integrations/github/api.py` (`request`, `permission_missing`), `GitHubAuth.token()`, `ProposalKinds`, `ProposalShell`.

**Produces:** `Fixes` (I34) and `Training` (I35) implementations in `training/`; the tools `RequestFix`, `RecordFixChoice`; `PlaceholderResolver`, `CiDispatcher`, `HookVerifier`, `HookPinner`, `GateFacts` (I30, I36); the training `HookLifecycle` and the `ci_dispatch` `HookRunner`; the policy `ActionAuthority`; the `training_policy` proposal kind and card; the `fixes`, `training_requests` and `policies` query providers; the routes of the overview's "Routes" rows for training tasks 7 and 8. Every type is contracts'.

**One source:** guards live only in `Fixes.guards` (the lifecycle and the policy authority call it, never repeat a check); placeholders are resolved only by `PlaceholderResolver`; templates are filled only by `fill_template` (hooks task 2); the policy limits are evaluated only by the policy `ActionAuthority` (the guard `policy_limit` calls it); the Retraining agent name comes from where `diagnoser` takes agent names; constants (`TRAINING_REQUEST_DEADLINE_MINUTES = 1440`, `TRAINING_REQUEST_GRACE_MINUTES = 30`, text caps) in `training/constants.py`.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | `RequestFix`, `RecordFixChoice`, the fixes store, the wake of Retraining | contracts 1, 2; incidents 1, 2 | after incidents task 2; early PR |
| 2 | The guards | task 1; incidents 1 | after task 1; early PR |
| 3 | `PlaceholderResolver` and the training `HookLifecycle` | task 2; hooks 1 | after task 2 and hooks task 1; early PR |
| 4 | `CiDispatcher` and the `ci_dispatch` runner | task 3; hooks 2 | after task 3 and hooks task 2 |
| 5 | Linking back by the request tag; the `training_request` expectation; Retraining woken on link | task 3; results 3, 5 | after task 4 and results tasks 3 and 5 |
| 6 | The circuit breaker, decline back to observe, the outcome of each fix | tasks 1, 5; notifications 1, 2 | after task 5 and notifications task 2; early PR |
| 7 | Pins, the alias-history check, suspension, the approve route, `GateFacts` | tasks 3, 5; hooks 1; results 3; contracts 3 | after task 6 |
| 8 | Training policies: kind, store, routes, card, the policy `ActionAuthority` | tasks 2, 7; contracts 3, 4; hooks 5 | after task 7, contracts task 4 and hooks task 5 |

Tasks 1 to 6 are the core path (fix → run → linked); 7 and 8 are the extras. Until task 7, `GateFacts` is contracts' default (all `None`), so every train or deploying run asks; until task 8 no policy exists, so `train` and `evaluate` ask at every level (I30). Both are the safe direction.

---

### Task 1: `RequestFix`, `RecordFixChoice`, the fixes store, the wake of Retraining

**Files:** new `backend/src/mlops_agents/training/fixes.py` (`SqliteFixes`, the `Fixes` implementation), `training/tools.py` (`RequestFix`, `RecordFixChoice`), `training/constants.py` (its own lines); `agents/tools/__init__.py` (one line: both tools, taking `fixes: Fixes | None`); `api/services.py` (construct, pass to `default_registry`); tests under `backend/tests/training/`.

**Interfaces:** consumes I34, `Incidents.get`, `AgentWaker.wake`, `untrusted`, the tool names. Produces `SqliteFixes(databases, incidents, waker, events, clock)` with `request`, `choose`, `get`; `guards` (task 2) and `circuit` (task 6) are added by their tasks, never stubbed, and until then `SqliteFixes` is typed as itself, not as `Fixes`.

**Notes:**
- `request`: the incident must exist and name a model (`FixRequestView.model` is required), else a user-facing refusal the tool returns as `ToolFailure`. A second request for an incident whose fix is still `requested`, `chosen` or `running` returns that fix and wakes no one again. Stores the fix (`fix_...`, status `requested`, outcome `pending`), records `fix.requested` with the incident as `subject_id`, then `AgentWaker.wake(project, <retraining>, "fix_request", subject_id=<incident>, text, key=<fix id>)`. The text names the fix, incident, model and the evidence through `untrusted(..., source="fix_request")` (the evidence quotes the team's data).
- `choose`: only Retraining (the caller's agent name against the one constant); only a fix in `requested`; `why` non-blank and `why_not` holding an entry for each menu option (`re_threshold`, `recalibrate`, `retrain`) other than the choice. `decline` sets `declined` and records `fix.declined` (task 6 adds what follows a decline); others set `chosen` and record `fix.chosen`. Free text is capped (constants) and stored as given; it never reaches another agent's prompt without `untrusted()`.
- Both tools are risk `read` (I33); their output is short text naming the fix ID and status.

**Tests (stop condition):** a request stores the fix, records the event on the incident and wakes Retraining once with label `fix_request` and the incident as subject (in-test `AgentWaker` double); a repeated request returns the same fix with no second wake; an unknown incident and an incident with no model are refused; the evidence reaches the wake text only inside the `untrusted()` block; `choose` by another agent is refused; `choose` on a fix not `requested`, with a blank `why` or a missing `why_not` entry is refused; `decline` and `retrain` set their statuses and events; both tools are registered and allowed at every level. Full suite, ruff, format, pyright; CI green.

**PR:** early PR from `plan/stage-5-training-task1` into `main` (`AGENTS.md` rule 5): definitions task 2 gives Retraining and the diagnosers these tools.

---

### Task 2: The guards

**Files:** `training/guards.py` (one function per guard, composed by `SqliteFixes.guards` in `training/fixes.py`); tests.

**Interfaces:** consumes `Incidents.list`, `Incidents.signals`, `Incidents.holds`, `Handoffs.current` (`ModelSpec.profile.label_maturity_days`, `data_roles`). Produces `Fixes.guards(project_id, model, window, policy_id) -> GuardResult`, every refusal listed (never only the first).

**Notes:**
- `data_held`: a hold from `Incidents.holds(model)` (this model's or a model-less incident's) whose `HoldScope` tables intersect the model's training tables (the handoff's `data_roles` with role `training`; empty `tables` means the whole model) and whose dates overlap the window (a `None` bound is open).
- `open_data_quality_incident`: an incident not `resolved`, `closed` or `merged`, category `data_quality`, whose signals' subjects include one of the model's training tables and whose span (`started_at` to now) overlaps the window. The subject matches by exact name, as decided point 6.
- `labels_immature`: `window.end` later than now minus `label_maturity_days`.
- `policy_limit` (task 8) and `circuit_open` (task 6) are added to this one function by those tasks. A run on held data is a data-loss-class mistake: test each guard on its boundary.
- S5: a fix that waits for a PR's merge is guarded again at its run, which holds because the lifecycle (task 3) calls `guards` at every run.

**Tests (stop condition):** each guard refuses on its own case and passes just outside it (a hold ending before the window, an incident on another table, a resolved incident, labels exactly at maturity); several refusals are all returned; no handoff refuses with a clear message rather than passing. Full suite and checks; CI green.

**PR:** early PR from `plan/stage-5-training-task2` into `main`.

---

### Task 3: `PlaceholderResolver` and the training `HookLifecycle`

**Files:** new `backend/src/mlops_agents/monitoring/hooks/placeholders.py` (`TrackerPlaceholderResolver`), `training/requests.py` (`SqliteTraining`: the `trq` store and `Training.request`, `requests`), `training/lifecycle.py` (`TrainingLifecycle`); `api/services.py` (`Hooks.add_lifecycle`); tests.

**Interfaces:** consumes `HookView`, `HookCall`, `HookResult`, `HookRefused`, `MissingPlaceholder`, `RunContext`, `Tracking.query(what="models")`, `Fixes.guards`. Produces `PlaceholderResolver` (I36), `Training.request` and `requests` (I35; `policies` arrives in task 8), and the lifecycle every hook run goes through.

**Notes:**
- The lifecycle's `before` handles every hook kind, so placeholders are resolved in one place: it builds `RunContext` from the hook (`model`), the call's `params` (`window_start`, `window_end`, `candidate_version`, `threshold`, given by Retraining or by promotion's apply) and, for `train` and `evaluate`, the new request (`request_id`, `mode`); then `PlaceholderResolver.resolve` returns exactly the hook's declared placeholders as strings, or raises `MissingPlaceholder` before anything runs. `champion_version` is `ModelSummary.production_version`, read at that moment and only when declared. A hook of another kind that declares `request_id` or `mode` is refused with `MissingPlaceholder` (`request_id` is `None` there).
- For `train` and `evaluate`, in this order, so a refused run leaves no row: take the window from the call (task 8 adds the policy's `WindowRule`; with neither, the run is refused: the guards need a window); run `Fixes.guards` (refusals become one `HookRefused` listing them); generate the ID with `new_id("trq")`; resolve the placeholders (`MissingPlaceholder` refuses); only then insert the request (`status="started"`, `mode` from the hook kind, `fix_id` from the call), record `training.requested`, and move a fix in `chosen` to `running`.
- `after`: a failed result sets the request `failed`; a started one is handed to task 5's expectation.
- **Security:** every value is rendered by the app from a typed value (an ID, an ISO 8601 UTC instant, a float, a model name or a version string matched against `^[A-Za-z0-9._:+-]{1,128}$`), never an agent's raw string: the team's command or workflow may interpolate it into a shell. A param or a name that does not match is refused.

**Tests (stop condition):** each placeholder filled from its source; a declared placeholder the context lacks raises `MissingPlaceholder` and nothing runs (in-test runner double records no call); `champion_version` read at run time (alias moved between two runs gives two values); a deploy hook gets `candidate_version` from the call's params; a rollback hook declaring `request_id` is refused; a `train` run creates one `trq` with `training.requested` and moves its fix to `running`; a `train` run without a window is refused; guard refusals give one `HookRefused` with every refusal and create no `trq`; an agent `window_start` that is not a date, and a version or model name with a space or `;`, are refused; a `MissingPlaceholder` on a `train` hook leaves no request row; a failed result marks the request `failed`. Full suite and checks; CI green.

**PR:** early PR from `plan/stage-5-training-task3` into `main` (promotion consumes the resolver through the lifecycle).

---

### Task 4: `CiDispatcher` and the `ci_dispatch` runner

**Files:** new `backend/src/mlops_agents/integrations/github/dispatch.py` (`dispatch_workflow`), `monitoring/hooks/ci_dispatch.py` (`GitHubCiDispatcher`, `CiDispatchRunner`); `tests/fakes/fake_github.py` (its own lines: `dispatches` state, `reset`, one route); `api/services.py` (`Hooks.add_runner`); tests.

**Interfaces:** consumes `request`, `permission_missing`, `GitHubAuth.token()`, `WorkspaceService.get`, `github_remote`, `fill_template`. Produces `CiDispatcher` (I36) and the `HookRunner` with `type = "ci_dispatch"`.

**Notes:**
- `dispatch_workflow(http, token, repo, workflow, ref, inputs)`: `POST /repos/{repo}/actions/workflows/{workflow}/dispatches` with `{"ref", "inputs"}`, `permission="actions: write"`; 204 is success; 422 (no `workflow_dispatch` trigger, unknown input) is a user-facing error naming the workflow.
- The ref is always the project's default branch (`Project.default_branch`); nothing in the hook, the params or the call can name another ref (S5 hard limit). A project with no GitHub remote returns `HookResult(ok=False)` saying the pipeline cannot be started until a CI workflow or a remote command exists (S5, stage 5 questions); not logged in returns the login message.
- Inputs: each `CommandSpec.inputs` value filled with the resolved placeholders by `fill_template`. `HookResult.detail` is scrubbed and capped; the token never appears in it, a log or an event. `request_id` is copied into `HookResult.request_id`; `ci_run_url` is `DispatchRef.run_url` (`None`: the API returns no run).

**Tests (stop condition):** with the fake GitHub: a dispatch posts the filled inputs on the default branch (request log); the call's params cannot change the ref; 422, 403 (`actions: write` missing) and unreachable each give `ok=False` with their message and no token in it; a local project without a remote is refused with the S5 message. Full suite and checks; CI green.

---

### Task 5: Linking back by the request tag; the `training_request` expectation; Retraining woken on link

**Files:** `training/linking.py` (`RequestLinker`, a `SyncObserver`), `training/requests.py` (link, silent), `training/queries.py` (the `fixes` and `training_requests` providers); `api/services.py` (observer, providers); tests.

**Interfaces:** consumes `SyncObserver`, `Scheduler.add_sync_observer`, `Expectations.register`, `retire`, `states`, `Tracking.query(what="runs")`, `REQUEST_TAG`, `AgentWaker`, `QueryProviders`. Produces `training.linked`, the request states `linked` and `silent`, Retraining's wake on link, and the two providers.

**Notes:**
- After a started request, `after` registers `ExpectationSpec(key=ExpectationKey(kind="training_request", ref=<trq>, model), category="operational", cadence=None, due_at=started + the policy's run_time_limit_minutes, or TRAINING_REQUEST_DEADLINE_MINUTES without a policy, source="training", timezone="UTC", grace_minutes=TRAINING_REQUEST_GRACE_MINUTES, second_deadline=False, activates_on_pr=None)`; expectations raises the silence signal, never training.
- `synced`: pages through `Tracking.query(what="runs", since=<oldest started request>)` to the end (`TrackingQuery` has no tag filter, so one default page misses tags on a busy tracker); for each `started` request, a run whose `tags[REQUEST_TAG]` equals its ID links it (`run_key`, `linked`, `training.linked` on the incident of its fix, expectation retired); a request whose expectation state is `silent` becomes `silent`. Idempotent: a second sync changes nothing. A fix's status becomes `done` when its request links.
- On link, Retraining is woken (`fix_request`, subject the fix's incident, key `linked:<trq>`) to read the evaluation and write the report; a request with no fix wakes no one.
- Providers render rows through the `MonitoringQuery` tool's `untrusted()` path (incidents'); `why`, `why_not` and evidence are free text.

**Tests (stop condition):** a tagged run in the fake MLflow links its request after one sync, also when 120 newer untagged runs precede it, and wakes Retraining once; a second sync changes nothing; an untagged run and a run tagged with another ID link nothing; the expectation is registered with the derived deadline and retired on link; a silent expectation marks the request `silent`; both providers page and filter. Full suite and checks; CI green.

---

### Task 6: The circuit breaker, decline back to observe, the outcome of each fix

**Files:** `training/outcomes.py` (an `IncidentObserver` and the outcome rule), `training/fixes.py` (`circuit`, the `circuit_open` guard, escalation); `api/services.py`; tests.

**Interfaces:** consumes `IncidentObserver`, `MonitoringSettings` (`circuit_breaker_fixes`, `circuit_breaker_window_days`), `Notifier`, `OnCallPeople`, `AgentWaker`. Produces `Fixes.circuit`, `fix.escalated`, outcomes `cleared` and `not_cleared`.

**Notes:**
- Outcome: a fix whose request failed or went silent is `not_cleared`; after a linked run, its incident `resolved` gives `cleared` and `reopened` gives `not_cleared`. A re-threshold done only by PR has no run: its fix stays `chosen` and takes its outcome by the same incident rule. Declined and escalated fixes do not count.
- `circuit` is open when the model has at least `circuit_breaker_fixes` `not_cleared` fixes created within `circuit_breaker_window_days`. While open, `request` stores the fix as `escalated`, does not wake Retraining, records `fix.escalated` with what was tried, and notifies the first on-call person by `escalation_order` (`Notifier.notify`, kind `escalation`, the incident as `incident_id`, not urgent); `guards` adds `circuit_open`.
- Decline: the incident stays open; `fix.declined` is on it; the requesting agent is woken (label `incident`, subject the incident, key the fix ID) with the reason inside `untrusted()`.

**Tests (stop condition):** each outcome transition; the circuit opens exactly at N within the window and not with an older fix outside it; an open circuit escalates without waking Retraining, notifies the first on-call person once, and refuses a run with `circuit_open`; a decline wakes the requester once. Full suite and checks; CI green.

**PR:** early PR from `plan/stage-5-training-task6` into `main`: the end of the core path, which definitions task 2 describes to Retraining.

---

### Task 7: Pins, the alias-history check, suspension, the approve route, `GateFacts`

**Files:** new `monitoring/hooks/pins.py` (`GitHookPinner`, the pin check), `monitoring/hooks/alias_check.py` (together `HookVerifier`), `monitoring/hooks/facts.py` (`HandoffGateFacts`), new `backend/src/mlops_agents/api/routers/training.py` (the approve route); `training/linking.py` (calls the alias check); `api/services.py` (`hooks.set_pinner`, called before `HandoffHooks`' startup reconcile so existing unpinned hooks are pinned at start; `authorities.set_facts`, the PR and sync observers); tests; regenerated `frontend/openapi.json`.

**Interfaces:** consumes `HookView.pins`, `HookPinner`, `Hooks.set_pinner`, `suspend`, `set_deploys_verified`, `approve`, `PrObserver`, `SyncObserver`, `Incidents.raise_signal`, `set_severity`, `Tracking.query(what="deployments" | "versions")`, `ModelSpec.environments`, `production_signal`, `promotion`, `GitRunner`, `AuthorityRegistry.set_facts`. Produces `HookVerifier`, `HookPinner`, `GateFacts` and `POST /api/projects/{id}/hooks/{hook_id}/approve` (person only, answers `HookView`).

**Notes:**
- **Pins:** hashes are sha256 of the git blob bytes (`git cat-file blob <commit>:<path>` through `GitRunner`), never the working tree: Windows `autocrlf` changes those bytes. The pinner reads the workflow file and the declared entry files at the default branch's head (a GitHub project: `git fetch origin <default_branch>`, authenticated, in the managed clone, then `origin/<default_branch>`; a local project: its local `refs/heads/<default_branch>`). Never `WorkspaceService.refresh` (it checks out, resets and cleans) and never a checkout or reset of its own. `Hooks.register` and `approve` call it through `set_pinner`.
- **Pin check,** on each sync for a project with pinned hooks: files are hashed only when that head moved since the last check; a failed fetch skips this sync's check with one log line and never suspends. Also on `PrObserver.changed` for a merged PR whose `files` touch a pinned path. A change: `Hooks.suspend` with the changed paths (task 8 also suspends the policies using it). **Spec against pins, on every pass (head moved or not):** the handoff lives in the app, not the repo, so a newer handoff can change a pinned hook's spec (a new workflow or entry file) without moving the head, and `Hooks.register` never re-pins an existing hook (hooks plan task 1). Each sync pass therefore also compares, for every hook with `approved_commit` set (the opposite of `register`'s "not pinned yet"), the paths its current spec uses with its pinned paths (a set comparison, no hashing; the paths come from one function in `monitoring/hooks/pins.py`, the one `GitHookPinner` uses to choose what to pin); a hook not pinned yet is `register`'s to pin, never a change (that first pinning, like a new hook's, accepts whatever is on the default branch then, with no person); a path that is not pinned is a change: `Hooks.suspend` with it, and `approve` pins it. Until that pass runs, the hook can run on the unpinned file (at most one sync interval); accepted.
- **Alias check,** after a linked run of a `deploys: false` hook: a production alias (one `ModelSpec.environments` maps to the production environment, or the `tracker_alias` production signal) that moved to a version registered by that run (`ModelVersionRow.run_key` equals the linked run) gives `moved`: suspend the hook, raise an `operational` signal for the model and set the incident `high`. Otherwise `set_deploys_verified(True)`. Any other production move (a person's promotion, the team's own CD, a version of another run) is not this run's doing and is ignored: a time-window match would suspend the hook on every legitimate promotion during a run. A model with no `production_signal` is never checked (the gate already asks).
- **`GateFacts.facts`:** (the hook deploys: `spec.deploys`, or `True` while the hook is suspended; the model has a `production_signal`; its `promotion` is `team_gate`), from the current handoff; `(None, None, None)` with no hook. Registered with `set_facts`.
- The approve route is a person's (session auth, never a tool); it calls `Hooks.approve`, which re-pins and clears the suspension.

**Tests (stop condition):** an unchanged file passes; a pinned hook whose spec now names an unpinned entry file is suspended on the next pass, with the default branch's head unchanged; a hook registered before a pinner existed is pinned at the next start and never suspended for having no pins (this closes the last item of #553's row in `docs/known-debt.md`; drop that clause at merge); a changed pinned file on the default branch suspends the hook (a test repo with CRLF content and `autocrlf=true` keeps the same hash); a merged agent PR touching a pinned path triggers the check; a `deploys: false` run whose version took the production alias suspends the hook and opens a high incident; a run with no move sets `deploys_verified`; a move of a non-production alias, and a production move to another run's version during the run, do not suspend; a failed fetch suspends nothing; `GateFacts` for each handoff case and for a suspended hook; the approve route re-activates the hook and is not reachable as a tool. Full suite and checks, `openapi_export --check`; CI green.

---

### Task 8: Training policies: kind, store, routes, card, the policy `ActionAuthority`

**Files:** `training/policies.py` (store, `Training.policies`, `TrainingPolicyKind`), `training/authority.py` (`PolicyAuthority`), `training/queries.py` (the `policies` provider), `training/lifecycle.py` (the policy's `WindowRule`), `monitoring/hooks/pins.py` and `alias_check.py` (suspend the hook's policies), `api/routers/training.py` (`GET /api/projects/{id}/training/policies`, `PUT .../training/policies/{pol}/enabled`; the approve route re-activates the hook's policies); `api/services.py` (kind, authority, provider); new `frontend/src/pages/agents/proposals/TrainingPolicyProposalCard.tsx` and its test, `frontend/src/pages/agents/ApprovalCard.tsx` (one line); regenerated `frontend/openapi.json`.

**Interfaces:** consumes `ProposalKind` (I19), `TrainingPolicyPayload`, `AuthorityRegistry.register("policy", ...)`, `Rules.list`, `Tracking.query(what="ci_runs")`. Produces `Training.policies`, the kind, `policy.suspended`, the routes, the card.

**Notes:**
- The `training_request` deadline (task 5's lifecycle) uses the policy's `run_time_limit_minutes` once a policy covers the request; today it always uses `TRAINING_REQUEST_DEADLINE_MINUTES` (the #575 row in `docs/known-debt.md`; the lead removes it at merge). A test.
- `PolicyAuthority.authorize`: basis `policy` only when the cited policy is `active`, its model is the call's, the hook is its train or evaluate hook, the last run is at least `min_interval_hours` old and fewer than `max_runs_per_week` ran in the last 7 days, counting every run of the pipeline, the team's own included, from `ci_runs` of the hook's workflow (a `command` hook has no CI history: the app's own `trq` rows for that hook are counted, and the card says so); and, for a hook that deploys, an enabled rollback rule exists for the model. Else basis `none` with the reason. The `policy_limit` guard calls this same function.
- The kind: key is the model; `warnings` state that the hook deploys and that the run-time limit is the team's to enforce; `apply` refuses a deploying hook without a rollback rule, is idempotent per proposal, and supersedes the model's previous policy (disabled).
- `WindowRule`: `trailing_days` is now minus `days` to now; `since_last_run` starts at the window end of the model's last linked request; `fixed` as given.
- The card uses `ProposalShell`: model, hooks, window rule, limits, warnings; Approve and Reject come from the shell.

**Tests (stop condition):** within limits the authority gives `policy`; the interval, the weekly cap (a team run in the fake `ci_runs` counts), another model's policy, a suspended policy and a deploying hook without a rollback rule each give `none` with their reason; a `command` hook counts its `trq` rows; the guard refuses the same cases; apply is idempotent and supersedes; a changed pin and a moved alias suspend the policy, and approving the hook re-activates it; each `WindowRule`; the routes; the card renders a payload and its warnings (vitest). Full suite, `npm test`, lint, `openapi_export --check`; CI green. No early PR: the last task.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
