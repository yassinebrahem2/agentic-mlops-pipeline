# Stage 4 — Hooks Implementation Plan

> **Draft.** Missing: the lead's last review of the stage 4-6 plan set after the consistency pass.

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1, 2 and 3 get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition). Read the [stages 4-6 overview](../2026-10-29-stage-4-6-overview.md) first: it holds the shared constraints and every interface named here (I23, I29, I30, I32, I33, "Routes"); this plan does not repeat them.

**Goal:** the team's own commands become action hooks (`hok`) the agents can run, and only through the gate (S4 §6): the handoff's commands are registered as hooks, a hook's credential reaches only its own container run, `RunHook` and `Hold` run them and record every outcome on the incident, and, last, pre-approved rules (`rul`) let a rollback, rerun or fall back run without asking when their condition holds in code against the stored results.

**Branch:** `plan/stage-4-hooks`.

**Consumes:**
- contracts task 1 (not on `main`): I29 in full (`monitoring/hooks/contract.py`: `HookView`, `HookCall`, `HookResult`, `HookRefused`, `HookRunView`, `HookRunner`, `HookLifecycle`, `HookPinner`, `Hooks`), I23's rules types (`monitoring/rules/contract.py`), `HandoffObserver` and `Handoffs.add_observer`, `hook_ref` and `hook_prefix`, the `hok` and `rul` prefixes, the event types `hook.ran`, `hook.failed`, `hook.suspended`, `rule.satisfied`, the tool names `RunHook` and `Hold`, `untrusted`, `QueryProviders` and `QueryProvider`, `IncidentView` and `HoldScope` (I26).
- contracts task 2 (not on `main`): the `hooks`, `hook_pins`, `hook_runs` and `rules` tables.
- contracts task 3 (not on `main`): I30 (`ActionCall`, `Authority`, `ActionAuthority`, `AuthorityRegistry`, `decide_action`, `action_call(args, ctx)`, `ToolContext.authority`, the gate that consults them).
- contracts task 4 (not on `main`; tasks 4 to 6): `HOOKS_KIND`, `RULE_KIND`, `HooksPayload`, `HookProposal`, `RulePayload`, their keys, `Condition` and `evaluate`, the widened `ProposalView`.
- incidents task 1 (not on `main`; task 3): `Incidents.record_hold`, `Incidents.get`.
- results task 2 (not on `main`; task 6): `Results.query`, `ResultQuery`, `StoredResult`.
- On `main`: `SqliteHandoffs.current` (I20), `ProposalKind` and `ProposalKinds` (I19), `ProjectSecrets`, `KeyStore`, `EventLog`, `Sandbox.rebuild` and `SandboxProjects.root`/`mounts`, `DockerRunner` (with its `secrets` scrubbing), `credentials/scrub.py`, `proposals/templates.py` (the template grammar, decided point 14), `config.ENV_PREFIX`, `sandbox/contract.py` (`MAX_BASH_TIMEOUT_S`), the stage 2 gate, `default_registry`, `build_services`, `include_routers`, `WorkspaceService.list_projects`.

**Produces:** `Hooks` (I29) as `HooksService`, with runner, lifecycle and pinner registration and `runs` (task 1, early PR, for training, promotion, notifications and stats); the `command` runner, the credential and enable routes, `fill_template` and `build_one_shot_run_args` (task 2, early PR, for training and results); the tools `RunHook` and `Hold` and the `hooks` provider (task 3, early PR, for promotion task 3, training and definitions); the `hooks` kind (task 4); `Rules` (I23) as `SqliteRules`, the `rule` kind, its routes (task 5); the rule `ActionAuthority` under citation `"rule"` and the `rules` provider (task 6). No type is defined here: every type above is contracts'.

**Constants (one source, `monitoring/hooks/constants.py`, created by task 1):** `HOOK_DETAIL_MAX_CHARS = 2000` (the cap on `HookResult.detail`); from task 2, `HOOK_CREDENTIAL_ENV = f"{ENV_PREFIX}HOOK_CREDENTIAL"` (the variable a hook's command reads its credential from; the definitions plan names it for agents writing commands) and `HOOK_COMMAND_TIMEOUT_S = MAX_BASH_TIMEOUT_S` (imported, not restated). Which caller `params` are allowed, and their values, are checked only by training's resolver (I36), never here.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | The `Hooks` store and runs, handoff registration, runner, lifecycle and pinner seams | contracts tasks 1 to 3 | after contracts tasks 1 to 3; **early PR** |
| 2 | Hook credentials, the `command` runner, the one-shot container run, `fill_template` | task 1 | after task 1; **early PR** |
| 3 | `RunHook`, `Hold`, failures on the incident, the `hooks` provider | task 2; incidents task 1 | after task 2 and incidents task 1; **early PR** |
| 4 | The `hooks` kind and card | task 1; contracts task 4 | after task 3 and contracts task 4 |
| 5 | The `Rules` store, the `rule` kind, routes and card | task 1; contracts task 4 | after task 4 |
| 6 | The evaluator and limits, the rule `ActionAuthority`, the timeline record | tasks 3, 5; results task 2 | after task 5 and results task 2 |

Tasks 1 to 3 are the core path (a hold and an approved rollback run through them); tasks 4 to 6 are extras. Early PRs go from `plan/stage-4-hooks-task<n>`, each created at that task's last commit.

---

### Task 1: The `Hooks` store and runs, handoff registration, runner, lifecycle and pinner seams

**Files:** new `backend/src/mlops_agents/monitoring/hooks/constants.py`, `monitoring/hooks/store.py` (rows of `hooks`, `hook_pins` and `hook_runs`), `monitoring/hooks/service.py` (`HooksService`), `monitoring/hooks/handoff.py` (`HandoffHooks`, the `HandoffObserver` and the startup reconcile); `api/services.py` (its lines); tests under `backend/tests/monitoring/hooks/`.

**Interfaces:** consumes I29, `HandoffObserver`, `Handoffs.current`, `hook_ref`/`ProjectSecrets.is_set` (for `credential_set`), `EventLog`, `Authority`. Produces `HooksService(databases, secrets, events, clock)` implementing `Hooks`, and `HandoffHooks(hooks, handoffs)` with `stored(project_id, handoff)` and `reconcile(project_ids)`.

**Notes:**
- Registration: every project-level `HandoffPayload.commands` entry (model `None`) and every `ModelSpec.commands` entry becomes a hook with `source="handoff"`, through `Hooks.register`, idempotent by (model, name). A handoff command's name is its kind (`rollback`), with `-2`, `-3` for later commands of the same kind and model in handoff order. A re-registration with a changed spec keeps the hook's ID, status and credential and replaces its spec. A `handoff` hook missing from a newer handoff is set `disabled`.
- `reconcile` runs once at startup in `build_services` over every project (`WorkspaceService.list_projects`), so projects whose handoff was stored before stage 4 get their hooks; one project's failure is logged and the others go on.
- `run(project_id, call, authority)`: a `disabled` hook raises `HookRefused`; a `suspended` hook is not refused here (the gate treats it as deploying through `GateFacts`, training task 7). Then each lifecycle's `before` in registration order, the runner for `spec.type` (none registered for it: `HookRefused` naming the type), each `after`. The runner is called once: no retry, ever (S4 §13). A runner exception becomes `HookResult(ok=False)` with the scrubbed message; a `HookRefused` is recorded and re-raised.
- Every run writes one `hook_runs` row when it starts (`HookRunView`'s fields: the call, the hook's kind and model, the authority's basis, ref and evidence) and completes it with the result, then one event: `hook.ran` when `ok`, else `hook.failed` (a refusal included), with `subject_id` the incident when the call has one, else the hook, and payload `{hook_id, run_id, kind, basis}`. `detail` is scrubbed (`credentials/scrub.py`) and cut at `HOOK_DETAIL_MAX_CHARS`. `runs(kinds, since, limit)` reads those rows, newest first.
- Pins: `set_pinner(pinner)` stores the pinner; `register` and `approve` call it when set and store its commit and pins in `hooks`/`hook_pins`. Until training task 7 sets one, `pins` is `[]` and `approved_commit` `None`.
- `set_enabled(True)` never clears a suspension; only `approve` does (and it re-pins). `suspend` records `hook.suspended`. `set_deploys_verified` stores the alias check's verdict.

**Tests (stop condition):**
- A stored handoff with one project command and one model command registers two `handoff` hooks named by their kind, `active`, `credential_set` false; two commands of one kind get `-2`; storing it again and a startup reconcile create nothing new (same IDs); a reconcile registers the hooks of a handoff stored before the observer existed.
- A newer handoff changing a command's template updates that hook in place (same ID, status and credential kept); one dropping a command disables its hook.
- `run` with fake runners and lifecycles: order before → runner → after; a `before` raising `HookRefused` skips the runner and records `hook.failed` on the incident; a failing runner is called exactly once; a disabled hook and a type with no runner are refused; the `hook_runs` row, `runs()` and the event carry the incident, the kind and the basis.
- A fake pinner's pins are stored on register and replaced on approve; `suspend` then `set_enabled(True)` leaves the hook suspended; `approve` clears it.
- Full backend suite, ruff, format, pyright; CI green.

**PR:** its own PR into `main` (`AGENTS.md` rule 5): training and promotion register runners, lifecycles and the pinner on it; notifications and stats read `runs`.

---

### Task 2: Hook credentials, the `command` runner, the one-shot container run, `fill_template`

**Files:** new `monitoring/hooks/command_runner.py` (`CommandRunner`), `backend/src/mlops_agents/api/routers/hooks.py`; `sandbox/containers.py` (one new function, `build_one_shot_run_args(project_id, image_tag, command, *, network: bool, mounts, env_file: Path | None)`, only); `proposals/templates.py` (`fill_template`, beside `check_template`); `monitoring/hooks/service.py` (`set_credential`, `delete_credential`); `api/services.py` (its lines); tests; regenerated `frontend/openapi.json`.

**Interfaces:** consumes `hook_ref`, `ProjectSecrets.put`/`delete`, `KeyStore` (read), `Sandbox.rebuild`, `SandboxProjects.root` and `mounts`, `DockerRunner`, `proposals/templates.py`. Produces `CommandRunner` (`type = "command"`) registered with `Hooks.add_runner`; `build_one_shot_run_args` (results task 6 reuses it for code parsers); `fill_template` (training's `ci_dispatch` runner reuses it); the routes `GET /api/projects/{id}/hooks` (`list[HookView]`), `PUT /api/projects/{id}/hooks/{hook_id}/credential` (body `{credential: str}`), `DELETE` on the same path, `PUT /api/projects/{id}/hooks/{hook_id}/enabled` (body `{enabled: bool}`), each returning `HookView`.

**Notes (security: get these exactly right):**
- The credential is stored only under `hook_ref(project_id, hook_id)` through `ProjectSecrets`, so project removal deletes it. It never appears in a response, a log, an event, a `hook_runs` row or an error.
- One container run per hook call: `docker run --rm` of the project's sandbox image (`Sandbox.rebuild` returns the ready tag, building only when missing), the repository root mounted read-only at `/workspace` and the connections' read-only mounts, the same CPU, memory and pid limits as a task container, with network (the team's approved command acts on the team's systems, such as moving the tracker's alias; the agent's Bash keeps the project's network setting). The command runs as `bash -lc <filled template>`, through `DockerRunner.run(args, timeout_s=HOOK_COMMAND_TIMEOUT_S, secrets=[credential])`.
- The credential reaches the container through `--env-file` pointing at a temporary file (owner-only, deleted in `finally`, also on timeout and cancellation) holding `HOOK_CREDENTIAL_ENV=<value>`; never as `-e NAME=value`, which puts it on the host's process list. An agent's Bash container never receives it. A hook without a credential gets no env file.
- Filling: `fill_template(template, values)` parses with `string.Formatter` and fills with `str.format_map` (decided point 14). The command runner passes each value through `shlex.quote` first, since values come from the tracker and from callers. A placeholder the template uses with no value raises `HookRefused` naming it, before Docker is called (until training's lifecycle fills placeholders, a template with any placeholder is refused this way).
- `ok` is exit code 0 and not timed out; `detail` is the scrubbed, capped output tail; `ci_run_url` is `None`; `request_id` is `params.get("request_id")`. Docker unavailable is `ok=False` with the `SandboxUnavailable` message.

**Tests (stop condition):**
- Routes: setting a credential makes `credential_set` true and the value is absent from every response body and from the recorded events; delete clears it; removing the project deletes the ref; enable and disable work; an unknown hook is a 404.
- `CommandRunner` with `FakeDockerRunner` (`tests/sandbox/fakes.py`): the args hold `--rm`, the image tag, read-only mounts, network, and `--env-file`, and no argument contains the credential; the env file exists during the call and is gone after success, a timeout and a raised error; output containing the credential comes back scrubbed; a value `x; rm -rf /` reaches the command as one quoted word; a missing placeholder is refused with no Docker call; a hook without a credential gets no env file; exit 1 and a timeout give `ok=False` with capped detail.
- `build_one_shot_run_args` with `network=False` and no env file gives `--network none` and no `--env-file`.
- Full backend suite, ruff, format, pyright, `npm run gen:api` committed; CI green.

**PR:** its own PR into `main` (`AGENTS.md` rule 5): results task 6 and training task 4 reuse its two functions.

---

### Task 3: `RunHook`, `Hold`, failures on the incident, the `hooks` provider

**Files:** new `backend/src/mlops_agents/agents/tools/run_hook.py` (`RunHookTool`), `agents/tools/hold.py` (`HoldTool`), `monitoring/hooks/query.py` (the `hooks` provider); `agents/tools/__init__.py` (`default_registry`: its parameters and lines); `api/services.py` (its lines); tests.

**Interfaces:** consumes `Hooks`, `Incidents.record_hold` and `get`, `action_call` and `ToolContext.authority` (I30), `untrusted`, `QueryProviders`. Produces `RunHook(hook_id, reason, incident_id?, fix_id?, params?, rule_id?, policy_id?)` and `Hold(incident_id, scope: HoldScope, reason)`, both risk class `deploy` (I33); the `hooks` query provider.

**Notes:**
- `RunHook.action_call` gives `kind` = the hook's `spec.kind`, `hook_id`, the hook's `model`, and `incident_id`, `rule_id`, `policy_id` from the arguments; `deploys`, `has_production_signal` and `team_gate_authority` stay `None` (the gate fills them from `GateFacts`, never from the agent). An unknown hook ID is a tool error and never reaches a runner.
- `run` calls `Hooks.run(call, ctx.authority)`: the `Authority` the gate decided on (rule, policy, or approval after an approved ask); a call reaching `run` without one is a bug and raises.
- A `params` key the hook does not declare as a placeholder is a tool error; which declared ones a caller may set, and their values, are training's resolver's to check.
- `Hold`: `action_call` gives kind `hold`, the incident's model, and the hold hook it will run (or `None`). It calls `Incidents.record_hold(..., by=agent)` first, then runs the hold hook with the same authority: the model's one active `hold` hook, else the project-level one. Several active hold hooks for that scope: it records the hold, runs none, and lists them so the agent picks one with `RunHook`. With none, the hold is recorded and the tool says no hold command is registered; the reactor sends the hold change's deliveries (incidents task 4). A failing hold hook leaves the hold recorded.
- A hook's output is the team's text: the tool returns `detail` only through `untrusted(..., source="hook_run")`. A failure is a tool error naming the hook, so the agent chooses again (S4 §13).
- The `hooks` provider pages `HookView` rows and filters by model.

**Tests (stop condition):**
- `action_call` for a rollback hook citing a rule carries the hook's kind and model and the rule ID; agent-supplied `deploys` is impossible (no such argument).
- Through the real gate (contracts task 3) with a fake runner: a `deploy` hook at `autonomous` asks and does not run until approved, then runs with basis `approval`; a `rollback` with no citation asks at `ask` and runs at `bypass`; `Hold` asks at `suggest` and runs at `ask`.
- `RunHook` on a failing hook returns a tool error and the incident's timeline shows `hook.failed`; the output reaches the agent wrapped by `untrusted()`; a `params` key the hook does not declare is refused.
- `Hold` with a model hold hook: the incident is `held`, the hook ran once with the incident ID; with none: held, no runner call, the message says so; with two: held, none run, both listed; with a failing hook: still held.
- The `hooks` provider pages and filters by model.
- Full backend suite, ruff, format, pyright; CI green.

**PR:** its own PR into `main` (`AGENTS.md` rule 5): promotion task 3 runs the deploy hook through `Hooks.run`, training's lifecycle is exercised through `RunHook`, and definitions task 1 gives the agents the tools.

---

### Task 4: The `hooks` kind and card

**Files:** new `monitoring/hooks/kind.py` (`HooksKind`), `frontend/src/pages/agents/proposals/HooksProposalCard.tsx` and its test; `frontend/src/pages/agents/ApprovalCard.tsx` (one `CARD_FOR_PROPOSAL` line); `api/services.py` (its line).

**Interfaces:** consumes `ProposalKind`, `HOOKS_KIND`, `HooksPayload`, `HookProposal` and its key (I32). Produces `HooksKind` registered in `ProposalKinds`.

**Notes:** `apply` registers each entry with `source="proposal"`, idempotent per proposal (registration is by (model, name)). `warnings` names every entry whose spec has `deploys: true` (I32). `credentials_needed` returns `[]`: a hook's credential is set on its route (task 2) once the hook exists. The card shows, per hook: name, model, kind, type, template or workflow with its inputs, placeholders, entry files, `deploys`, why; and the warnings as given.

**Tests (stop condition):** apply registers the hooks and a second apply changes nothing; a `deploys: true` entry produces its warning; the card renders a `command` and a `ci_dispatch` entry and the warning; `ApprovalCard` picks the card for kind `hooks`; backend suite, `npm test`, lint, format; CI green.

---

### Task 5: The `Rules` store, the `rule` kind, routes and card

**Files:** new `backend/src/mlops_agents/monitoring/rules/store.py` (`SqliteRules`), `monitoring/rules/kind.py` (`RuleKind`), `api/routers/rules.py`, `frontend/src/pages/agents/proposals/RuleProposalCard.tsx` and its test; `ApprovalCard.tsx` (one line); `api/services.py` (its lines); tests; regenerated `frontend/openapi.json`.

**Interfaces:** consumes I23 (`RuleSpec`, `RuleView`, `Rules`), `RULE_KIND`, `RulePayload` and its key, `Hooks.get`. Produces `SqliteRules` implementing `Rules`, `RuleKind`, `GET /api/projects/{id}/rules` (`list[RuleView]`, `?model=`) and `PUT /api/projects/{id}/rules/{rule_id}/enabled` (`RuleView`).

**Notes:** `apply` checks in code that `spec.hook_id` exists, has kind `spec.hook_kind` and the rule's model; otherwise it fails with the reason in `ApplyResult.failed`, never stores. `store` is idempotent per proposal; a new rule starts enabled. `warnings` states it when the rule's hook deploys (I32). The card renders each term (check, model, condition, window, quantifier, minimum records), the hook and the limits.

**Tests (stop condition):** apply stores an enabled rule and a second apply changes nothing; a missing hook, a hook of another kind and a hook of another model each fail the apply; disable and enable through the route; the card renders terms, hook and limits; backend suite, `npm test`, lint, format, `npm run gen:api` committed; CI green.

---

### Task 6: The evaluator and limits, the rule `ActionAuthority`, the timeline record

**Files:** new `monitoring/rules/evaluator.py`, `monitoring/rules/authority.py` (`RuleAuthority`), `monitoring/rules/query.py` (the `rules` provider); `monitoring/hooks/service.py` (the limit re-check and `rule.satisfied`); `api/services.py` (its lines); tests.

**Interfaces:** consumes `Rules`, `Results.query`, `evaluate` (I23, the one evaluator), `ActionCall`, `Authority`, `AuthorityRegistry`, `Clock`, `QueryProviders`. Produces `RuleAuthority` registered with `AuthorityRegistry.register("rule", ...)`, `rule_limits_allow(...)` (one function, used by the authority and by `Hooks.run`), the `rules` provider.

**Notes (the hard limit: an agent cites a rule, it never certifies one):**
- `authorize(call)` returns basis `"rule"` only when: the rule exists, is enabled, its `hook_id`, `hook_kind` and `model` equal the call's; the call has an `incident_id` (the per-incident limit needs one); every term holds; the limits allow. Otherwise basis `"none"` with the reason. Nothing from the agent (its reason, its claim) is read.
- A term reads `Results.query(check_id, model, since=now - window_minutes)` and evaluates `when` on each stored record's fields (`detail.<key>` addresses detail); fewer than `min_records` records: the term fails; `latest`: the newest holds; `any`: one holds; `all`: every one holds. `evidence` is the IDs of the records that satisfied the terms.
- Limits count `hook_runs` rows whose basis was `rule` and whose `rule_id` is this rule: on the incident (`per_incident`) and in the 24 hours before now (`per_day`: stricter than a calendar day, and needs no time zone). `Hooks.run` repeats `rule_limits_allow` and inserts its started run row under one per-project lock, so two calls in one step cannot both pass a limit of 1.
- When a run proceeds with basis `rule`, `Hooks.run` records `rule.satisfied` on the incident with the rule ID and the evidence (S4 §6: the timeline records the rule and the records).

**Tests (stop condition):**
- Evaluator: each quantifier, `min_records`, window edges with the fake clock, a missing field is false, another model's records are not counted.
- Authority: basis `rule` with evidence when everything holds; basis `none` for a disabled rule, another hook, another kind, another model, no incident, a failing term, `per_incident` reached, `per_day` reached.
- Through the real gate: `RunHook` citing a holding rule at `ask` runs without an approval and the timeline shows `rule.satisfied` with the result IDs; at `suggest` it is denied; a failing condition asks; a `deploy` hook can never be cited (no rule has that kind).
- Two `RunHook` calls citing the same rule with `per_incident=1` in one step: exactly one runs.
- The `rules` provider pages and filters by model.
- Full backend suite, ruff, format, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
