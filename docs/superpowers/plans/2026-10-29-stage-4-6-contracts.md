# Stages 4 to 6 — Contracts Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Tasks 1 to 4 each get an early PR (`AGENTS.md` rule 5); task 5 is pushed to the plan branch and the plan PR closes it. Each task lists its files, the interfaces it consumes and produces, and its tests (its stop condition).

**Goal:** land, once, what every stage 4-6 plan shares, so each of them only adds its own module: the IDs, event types, constants, reads and seams of "Shared by contracts"; every type of I22 to I42; the schema; the gate's second input (I30); the condition language and the six proposal payloads (I23, I32). A user sees nothing new: every default keeps today's behaviour until the plan that owns it replaces it.

**Branch:** `plan/stage-4-6-contracts`.

**Overview:** [`2026-10-29-stage-4-6-overview.md`](../2026-10-29-stage-4-6-overview.md). Read it first, in full; this plan does not repeat it. "Ix exactly" below means: the names, fields, types and comments of that interface as the overview writes them.

**Consumes (all on `main`):** stage 3's I13 to I21 (I16 `Page`, `EvaluationRecord`, `DeploymentRow`; I19 `ProposalView`, `ProposalKind`, `CommandSpec`, `Cadence`, `Feed`, `OnCall`, `OnCallHours`, `CheckSpec`, `FileResults`, `MlflowResults`, `PipelineEdge`, `HandoffPayload`, `TimeZoneName`; I20 `Handoffs`, `HandoffView`, `SqliteHandoffs`; I21 `Clock`); stage 2's `ApprovalGate`, `GateCall`, `GateDecision`, `AgentTool`, `ToolContext`, `PermissionLevel`, `PolicyDecision`, `AgentState`, `PrSyncObserver`, `TaskView`, `EventLog`, `AuditLog`. Task 4 also consumes the stage 3 proposals plan (merged in #444; Decision #447).

**Produces:** "Shared by contracts" in full; every type of I22 to I42; both migrations and the `MonitoringSettings` accessor and route; I30's behaviour (`AuthorityRegistry`, `decide_action`, `PROPOSE_BYPASS_OVERRIDES`, the gate change, `ToolContext.authority`); I23's `evaluate`; I32's payloads, kind names, `ProposalCheck`; I19's `ProposalView.payload` widened; `Propose` taking every registered kind.

**Who writes what.** Contracts writes every name in the I22 to I42 code blocks except the functions the overview gives another producer: the times functions of I24 (results task 1), `comparable` (promotion task 1), `cost_usd` (ui task 1). A `# results`-style comment on a Protocol names its implementer; the Protocol itself is contracts'. Contracts writes no implementation of a Protocol except the defaults named below and the two event-log reads.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Shared by contracts and every type of I22 to I42 | merged code | now; early PR |
| 2 | The schema: both migrations, `tables.py`, the settings accessor and route | task 1 | after task 1; early PR |
| 3 | The gate's second input (I30) and its defaults | task 1 | after task 1; early PR |
| 4 | `expr`, the payloads, the kind names, the widened `ProposalView`, `Propose` for every kind | tasks 1, 3 | after tasks 1 and 3; early PR |
| 5 | Consistency tests and the regenerated API contract | tasks 1 to 4 | after task 4; no early PR (last task) |

All five are core path: results, incidents, notifications, hooks and ui task 1 start on tasks 1 and 2; hooks and training on task 3; every proposal card on task 4.

---

### Task 1: Shared by contracts and every type of I22 to I42

**Files** (backend paths under `backend/src/mlops_agents/`):
- `ids.py` and `tests/test_ids.py`: the twelve prefixes (the roadmap already lists them).
- `config.py`: `CRONITER_VERSION`; `backend/pyproject.toml` (`croniter==<CRONITER_VERSION>`) and `uv.lock` (`uv lock`, never by hand).
- `events/contract.py` (`EventType`, `EventLog.page`, `AuditLog.page`), `events/log.py`, `events/audit.py` (the two reads), `frontend/src/lib/useProjectEvents.ts` (`PROJECT_EVENT_TYPES`): the stage 4 and 5 event types; regenerated `frontend/openapi.json` (`EventRecord.type` reaches the API).
- `credentials/project_secrets.py`: `hook_prefix(project_id)`, `hook_ref(project_id, hook_id)`.
- `agents/pr_sync.py`: `PrChange`, `PrObserver`, `add_observer`, the `changed` calls. `integrations/github/pulls.py`: `PullRequest.merged_at` and `changed_files`, `PullFile`, `list_pull_files` (every page); `tests/fakes/fake_github.py`: `merged_at` on merge, `changed_files`, and `GET /repos/{o}/{r}/pulls/{n}/files` (paged, with `previous_filename` on a rename).
- `proposals/contract.py` (`HandoffObserver`, `Handoffs.add_observer`, `ProposalCheck`) and `proposals/handoffs.py` (`add_observer` and the call after commit only).
- `monitoring/untrusted.py` and `agents/hooks/registry.py` (its `_block` calls `untrusted`; the data-tag rule moves with it).
- New constant and seam modules: `monitoring/settings.py` (`MonitoringSettings`, `MAX_WAKES_PER_HOUR = 30`), `monitoring/jobs.py`, `monitoring/query.py` (I33's query types, `QueryProviders`), `agents/tools/names.py`, `training/constants.py`, `promotion/constants.py`; `api/services.py` (contracts' lines: `Services.query_providers`, and the one `maintenance = NeverSuspended()` every stage 4 service is given, which incidents task 7 replaces).
- The package `__init__.py` files the overview lists.
- Type modules (new), one per area:

| Module | Holds |
|---|---|
| `monitoring/results/contract.py` | I22 exactly, with `source_id` and `parse_source_id` |
| `monitoring/expr.py` | I23's `Compare`, `AllOf`, `AnyOf`, `Not`, `Condition` (models only; task 4 adds the limits and `evaluate`) |
| `monitoring/rules/contract.py` | I23's rules |
| `monitoring/jobs.py`, `monitoring/scheduler/contract.py` | I24: `JobKind`, `JOB_ORDER`, `JobDue`; `HeartbeatView`, `SyncObserver`, `ScheduledJob`, `Scheduler` |
| `monitoring/expectations/contract.py` | I25 with `SECOND_DEADLINE_KEY`, and `NeverSuspended` (the `Maintenance` default: always `False`) |
| `monitoring/incidents/contract.py` | I26; I27's types, `diagnoser` and `MediumSeverity` (the `SeverityPolicy` default); `cause_key`, `ACTIVE_STATUSES`, `stages_of` |
| `notifications/contract.py` | I28, with the two keystore refs and the three app setting keys |
| `monitoring/hooks/contract.py` | I29, I36 |
| `agents/approvals/authority.py` | I30's `ActionKind`, `ActionCall`, `Authority`, `ActionAuthority`, `GateFacts` (task 3 adds the rest) |
| `monitoring/verdicts/contract.py` | I31 (with `VerdictDraft`'s validator) |
| `training/contract.py` | I34, I35 (with `TrainingPolicyPayload`, which `TrainingPolicyView` needs) |
| `promotion/contract.py` | I37 (with `PromotionPayload`), I38's `ProtectionSurfaces` and `MergeCheck`, I40 |
| `stats/contract.py` | I41 |
| `monitoring/contract.py` | I39 |
| `providers/usage_contract.py` | I42's three models |

- Tests: `backend/tests/test_stage4_6_contracts.py` (new), `tests/agents/test_pr_sync.py`, the handoffs tests under `tests/proposals/`, `tests/fakes/test_fake_github.py`, `tests/events/` (the reads), the context-hook tests (unchanged output).

**Interfaces:** consumes the stage 2 and 3 names above. Produces "Shared by contracts", every type of I22 to I42, `NeverSuspended`, `MediumSeverity`, `QueryProviders`, the two observer seams, the two page reads.

**Notes:**
- Every payload and view model is `extra="forbid"`, except `EvaluationPayload` (`extra="allow"`, I37). No contract module imports `proposals/contract.py`, so task 4 can import the payloads from it without a cycle; `IncidentView.verdict` imports `VerdictView` from the verdicts contract.
- `untrusted(text, source)` is the existing `<data source="...">` block of `agents/hooks/registry.py`, moved, with its data-tag rule (a `<data` or `</data` inside the text, any case or spacing, becomes `<\`): text holding the end tag cannot close the block early. The context hooks' output does not change.
- `cause_key(subject, model)`: two different `(subject, model)` pairs never give the same key (a separator inside `subject` included). `source_id(kind, ref, connection)` is the overview's three formats and nothing else builds them; `parse_source_id` is its inverse. `stages_of` is an exact name match (decided point 6).
- `diagnoser` returns the packaged definition names (`data-quality`, `model-performance`, `retraining`, `deployment`, `orchestrator`) per I27's mapping (subtype `silence` and any other operational subtype: `orchestrator`).
- `MonitoringSettings`: the overview's defaults; `collection_interval_s` at least 10; `timezone` a `TimeZoneName | None`.
- `QueryProviders.register` raises on a second provider for one `what`; built once in `build_services` before any plan's lines, as is the one `Maintenance` instance.
- **Event reads:** `EventLog.page` and `AuditLog.page` exactly as the overview; keyset, newest first, `before` exclusive; the existing `history` stays as it is.
- **Handoff seam:** `SqliteHandoffs.store` awaits each observer after its transaction commits (an idempotent repeat included: observers are idempotent), passing `current()` (the newest version), never the row just stored. An observer that raises is logged at warning with the project; the stored handoff stands and the other observers still run (the startup reconcile repairs it).
- **PR seam:** for a task whose PR is merged or closed, the sync lists the PR's files first (`list_pull_files`: old and new path of a rename), then ends the task, then awaits each observer with `PrChange(number, state, merged_at, head_branch=task.branch, agent=task.agent, files)`. A failure listing files or ending the task skips that PR this round (the task stays open, so the next sync retries and no change is lost). `changed` fires once per transition; `open` is never sent by this sync. An observer that raises is logged and the others still run.

**Tests (stop condition):**
- Each new module imports; each new Protocol has exactly the overview's method names (as `tests/test_stage3_contracts.py` checks I13 to I18).
- `new_id` accepts each new prefix.
- `untrusted`: wraps text; text holding `</data>` in any case or spacing cannot end the block; the context hooks render as before.
- `cause_key`: equal inputs give equal keys; `("a|b", None)` and `("a", "b")`-style collisions give different keys. `source_id` and `parse_source_id` round-trip the three formats. `stages_of` on a fixture handoff.
- `diagnoser`: every category and the operational subtypes.
- `NeverSuspended` is `False` for a key and a connection name; `MediumSeverity` returns `medium`; `QueryProviders` refuses a duplicate.
- `JOB_ORDER == tuple(JobKind)`; `MonitoringSettings()` holds the defaults; `collection_interval_s=5` is refused.
- `VerdictDraft`: `wrong_type` without `right_category`, and `real_issue` with one, are refused.
- Event reads: by subject, by type, by source; paging returns no duplicate and no gap when a record arrives between pages; audit by actor, keyset on `(ts, id)` with equal `ts`.
- Handoffs: `store` calls each observer once after commit with the current view; storing an older proposal again after a newer one delivers the newer handoff; a raising observer leaves the version stored and the next observer called.
- `importlib.metadata.version("croniter") == CRONITER_VERSION` (as `test_duck_lockdown.py` pins DuckDB).
- PR sync (fake GitHub): a merged PR gives one `changed` with `merged`, `merged_at`, the branch, the agent and the files (a rename gives both paths); a closed one gives `closed`; a second sync gives nothing; a files-listing failure leaves the task open and calls no observer, and the next sync delivers it; `list_pull_files` reads every page.
- `npm test` (the `PROJECT_EVENT_TYPES` contract test), `npm run lint`, `npm run build`; `python -m mlops_agents.api.openapi_export --check`; full backend suite, ruff, format, pyright; CI green.

**PR:** early PR into `main` from `plan/stage-4-6-contracts-task1` (`Closes #<task issue>`), as soon as it is done: six plans start on it.

---

### Task 2: The schema: both migrations, `tables.py`, the settings accessor and route

**Files:** `storage/migrations/project/<n>_stage4_5.sql` (`<n>`: the overview's rule), `storage/migrations/app/0003_stage4_5.sql`, `storage/tables.py`, `monitoring/settings.py` (`get_monitoring_settings`, `set_monitoring_settings`), `api/routers/monitoring_settings.py`; the schema-pin tests `tests/storage/test_migrate.py`, `test_stage2_schema.py`, `test_stage3_schema.py` (their version lists gain `<n>`); new tests under `backend/tests/storage/` and `backend/tests/api/`; regenerated `frontend/openapi.json`.

**Interfaces:** consumes task 1's models. Produces every table and column below, the price columns on `models`, the settings accessor and `GET`/`PUT /api/projects/{id}/monitoring-settings`.

**Notes:**
- **Migration number:** `0007` once approvals-fixes' `0006` (#390) is on `main`; `load_migrations` refuses gaps, so the PR takes the next free number on `main` when it opens, and whichever of the two PRs merges second renumbers its own file. Only the schema-pin tests name it.
- Only `CREATE TABLE`, `CREATE INDEX` and `ALTER TABLE ... ADD COLUMN`: nothing existing is dropped or rebuilt (no data loss); stage 3's `evaluations` is not recreated. New columns on an existing table go last, as `test_tables.py` compares column order.
- Column rules: a table's columns are its view model's fields unless listed otherwise (the ID as primary key; nested models, lists and dicts as JSON text; datetimes as ISO text; bools as `0`/`1` with a `CHECK`; every `Literal` as a `CHECK ... IN`). A unique key over a nullable column is an expression index on `coalesce(<column>, '')`, since SQLite treats NULLs as distinct.
- **`project.db` tables** (beyond the view fields, as listed):
  - `results`: `StoredResult`, plus `delivered_at` (null until every observer returned); unique (`source_id`, `source_key`); index (`check_id`, `model`, `ran_at`), index (`subject`, `model`, `ran_at`).
  - `result_sources`: `SourceState`. `parsers`: `ParserVersion` (spec JSON), unique (`source_id`, `version`), unique `proposal_id`.
  - `expectations`: the key fields, `ExpectationSpec` JSON, `active`, `last_seen`, the last occurrence signalled at the first and at the second deadline; unique (`kind`, `ref`, `coalesce(model,'')`).
  - `scheduler_jobs`: `JobDue` plus `started`, `finished`, `error`; unique (`kind`, `due`, `subject`).
  - `checks`: `CheckView`; unique `check_id`.
  - `signals`: `SignalView`; unique `result_id` where not null (one signal per result, so a re-delivered result raises nothing twice).
  - `incidents`: `IncidentView` except `signal_ids` (read from `signals`) and `verdict` (read through `Verdicts`), plus the hold scope (`HoldScope` JSON) and hold reason.
  - `incident_follow_ups`: ID, incident, agent, due, `after_days`, `after_labels`, label base count, note, done.
  - `recent_changes`: kind (`agent_pr`, `ci_run`, `deployment`), ref, at, detail JSON; unique (`kind`, `ref`) (incidents' suspects).
  - `agent_wakes`: agent, label, subject, key, text, created, `posted_at` nullable; unique (`agent`, `label`, `subject`, `key`).
  - `maintenance_windows`: `win` ID, `WindowSpec` fields, `proposal_id` (nullable, unique where set), created by, created.
  - `on_call_people`: ID, name, contacts JSON, hours JSON, timezone, escalation order.
  - `notification_log`: ID, incident (nullable), person, channel, kind, escalation step, status (`sent`/`retrying`/`failed`), attempts, next attempt, last error (scrubbed), message JSON, created, sent.
  - `monitoring_settings`: key, JSON value.
  - `hooks`: `HookView` except `pins` (from `hook_pins`) and `credential_set` (from the keystore); unique (`coalesce(model,'')`, `name`). `hook_pins`: hook, path, sha256, commit. `hook_runs`: `HookRunView`.
  - `rules`: `RuleView` (spec JSON), unique `proposal_id`.
  - `verdicts`: `VerdictView`, primary key `incident_id`. `silent_alert_verdicts`: `result_id` primary key, value, by, created. `misses`: an integer ID and `MissReport`. `baseline_links`: `BaselineLink`, unique (`incident_id`, `team_check_id`).
  - `fix_requests`: `FixRequestView`. `training_requests`: `TrainingRequestView`. `training_policies`: `TrainingPolicyView` (payload JSON), unique `proposal_id`.
  - `reports`: ID, the `ReportDraft` fields (assessments JSON), author, created (the evaluation is read, never stored).
  - `promotions`: `PromotionView` plus `proposal_id` (nullable, unique where set); unique (`model`, `candidate_version`) where `mode = 'team_gate'`.
  - On the existing `events`: an index on (`subject_id`, `seq`).
- **Idempotent applies:** every table a proposal kind writes has a unique `proposal_id` (parsers, rules, maintenance windows, training policies, promotions). Stage 3 re-applies at startup; without it a re-apply could run a deploy hook twice.
- `app.db`: `models` gains `price_prompt_per_mtok REAL`, `price_completion_per_mtok REAL`, `price_source TEXT CHECK (price_source IN ('provider','user'))`, all three null or all three set (one `CHECK`). Discord, email and `public_url` are rows of the existing key-value `settings` table under task 1's keys, so they need no DDL.
- The accessor reads every row of `monitoring_settings` over `MonitoringSettings()`'s defaults (a missing key is its default) and writes only through `MonitoringSettings` validation; no plan writes its own SQL against that table. Modelled on `agents/approvals/settings.py`. The route returns `MonitoringSettings` and takes a whole one (a 422 names the invalid field).

**Tests (stop condition):**
- A fresh `project.db` and `app.db` migrate to the latest version; one at the previous version migrates without losing a row of an existing table.
- Every table and column of the migrations matches `tables.py` (as the stage 3 contracts test does).
- Each unique key refuses a duplicate, the `coalesce` ones with a null model included (results, expectations, scheduler jobs, signals by result, agent wakes, hooks, baseline links, `proposal_id` on the five tables, team-gate promotions).
- The price `CHECK` refuses a partial price.
- The accessor: defaults on an empty table; a set value read back; an invalid value refused before any write. The route: round trip, 422, a session needed.
- `openapi_export --check`; full backend suite, ruff, format, pyright; CI green.

**PR:** early PR into `main` from `plan/stage-4-6-contracts-task2` (`Closes #<task issue>`): results, incidents, notifications, hooks and ui start on it.

---

### Task 3: The gate's second input (I30) and its defaults

**Files:** `agents/approvals/authority.py` (`AuthorityRegistry` with `register` and `set_facts`, `NoAuthority`, `NoGateFacts`), `agents/policy/actions.py` (new: `decide_action`, `PROPOSE_BYPASS_OVERRIDES`), `agents/approvals/gate.py`, `agents/runtime/hooks.py` (`GateCall.action`, `GateDecision.authority`), `agents/runtime/tool_step.py` (`_run_decided` only: the context it builds carries `decision.authority`), `agents/tools/base.py` (`AgentTool.action_call`, `ToolContext.authority`), `api/services.py` (contracts' lines: the registry, the gate's argument, `Services.authorities`); tests `backend/tests/agents/test_gate_actions.py`.

**Interfaces:** consumes task 1's I30 types. Produces I30 exactly: `AuthorityRegistry.register` and `set_facts`, `decide_action`, `PROPOSE_BYPASS_OVERRIDES`, `AgentTool.action_call`, `GateDecision.authority`, `ToolContext.authority`; the defaults `NoAuthority` (basis `none`) and `NoGateFacts` (all `None`). Rules and training register their authorities on `Services.authorities`; training task 7 calls `set_facts`.

**Notes:**
- `async def action_call(self, args, ctx) -> ActionCall | None` on `AgentTool`, default `None` (async: `RunHook` reads its hook). `tools/base.py` imports `ActionCall` under `TYPE_CHECKING` only: `policy/table.py` already imports `tools/base.py`.
- `GateCall.action` is an async method (`await self.tool.action_call(self.args, self.ctx)`), not a constructor field, so `decide` in `tool_step.py` is not touched.
- For a call whose `action` is not `None`, the gate overwrites `deploys`, `has_production_signal` and `team_gate_authority` from the registry's facts, whatever the tool set. It consults the authority registered for the citation the kind takes (`rule_id` for `rollback`, `rerun`, `fall_back`; `policy_id` for `train`, `evaluate`); no citation, the other citation type, or no registered authority gives `Authority(basis="none")`. Then `decide_action(level, call, authority)` replaces `policy_decide` for that call. Every other call is decided exactly as today.
- `decide_action` writes out I30's table. A `None` fact never selects the more permissive row: `train`/`evaluate` whose `deploys` is not `False`, and every other kind except `deploy` and `merge_pr` whose `deploys` is `True` (a suspended hook), take the "run that deploys" rows; only `team_gate_authority is True` with `has_production_signal is True` and basis `policy` gives the permissive one.
- The decision's `authority`: the `Authority` the call was allowed on; after an approved ask (`gate.wait`'s `approved` decision), `Authority(basis="approval", ref=<approval ID>, reason=..., evidence=[])`. `_run_decided` in `tool_step.py` builds the tool's context with `authority=decision.authority` for `allow` and `approved` (`None` for a call with no `action`). The decisions sit in the turn's checkpointed state, so `GateDecision.authority` must survive the checkpoint serializer: a test resumes a turn after an approval.
- At `bypass`, a `propose` call whose proposal kind is in `PROPOSE_BYPASS_OVERRIDES` takes that decision; every other propose call still asks.
- An approved `promotion` apply is not gated again (I30); nothing in this task routes it through the gate.

**Tests (stop condition):**
- `decide_action` for every cell of I30's table (parametrised over the four levels), including the `None`-fact and suspended-hook cases.
- Gate:
  - a fake `deploy`-class tool whose `action_call` claims `deploys=False` is decided from `GateFacts`, not from the claim;
  - a cited rule with a registered authority answering `rule` allows a `rollback` at `ask`, and the tool sees `ctx.authority.basis == "rule"`; one answering `none` asks;
  - an approved ask gives the tool `ctx.authority.basis == "approval"` with the approval ID, also after the turn is resumed from its checkpoint;
  - a `policy_id` on a `rollback` is ignored (basis `none`);
  - a `merge_pr` asks at `autonomous` and is allowed at `bypass`;
  - `hold` asks at `suggest`;
  - at `bypass` a `promotion` propose call is allowed and a `rule` propose call asks;
  - `set_facts` replaces the default.
- Registering a citation twice raises.
- Every existing gate test passes unchanged; full backend suite, ruff, format, pyright; CI green.

**PR:** early PR into `main` from `plan/stage-4-6-contracts-task3` (`Closes #<task issue>`): hooks and training start on it.

---

### Task 4: `expr`, the payloads, the kind names, the widened `ProposalView`, `Propose` for every kind

**Files:** `monitoring/expr.py` (limits, `evaluate`); the payloads in their owners' contract files (`ParserPayload` in `monitoring/results/contract.py`, `RulePayload` in `monitoring/rules/contract.py`, `HookProposal` and `HooksPayload` in `monitoring/hooks/contract.py`, `WindowSpec` and `MaintenancePayload` in `monitoring/incidents/contract.py`; `TrainingPolicyPayload` and `PromotionPayload` are task 1's); `proposals/contract.py` (the six kind constants, one key function per kind, `ProposalView`); `proposals/service.py` (`_to_view`'s construction and the apply of an allowed propose only); `agents/tools/propose.py` (`ProposeArgs`, `preview`, `run`); regenerated `frontend/openapi.json`; tests `backend/tests/monitoring/test_expr.py`, `tests/proposals/`, `tests/agents/tools/` for `Propose`.

**Interfaces:** consumes task 1's types, task 3's `PROPOSE_BYPASS_OVERRIDES` and I19. Produces I23's `evaluate`, I32 exactly (kind names beside `CONNECTIONS_KIND`, the keys the overview gives per kind), the widened `ProposalView`, `ProposeArgs` for every registered kind, `ProposalCheck` honoured by `Propose`.

**Notes:**
- `Condition` validation: depth at most 4, at most 32 nodes, refused at parse time. `evaluate` is pure, never `eval`; a missing field is `False`; `detail.<key>` reads the `detail` dict; comparing different types (a string with a number) is `False`, never an exception.
- `ProposalView` (Decision #447): one view model per kind, each with `kind: Literal[<its constant>]` and `payload: <its model>`, the shared fields on one base, combined with `Field(discriminator="kind")`, so the generated schema carries a discriminator and the frontend narrows by `kind`. The frontend reads `components["schemas"]["ProposalView"]` (`useProposal.ts`), so `openapi.json` must keep a `ProposalView` component (a `RootModel` over the union keeps it); if it cannot, change that one type line and say so in the PR. `_to_view` in `proposals/service.py` is the only construction site. The existing connections and handoff cards compile and pass unchanged.
- `ProposeArgs.payload` becomes a plain object; `Propose.preview` checks `kind` against the registered `ProposalKinds` and validates the payload with that kind's `payload_model` before the draft is created, so registering a kind is enough and the tool schema is not an untagged eight-way union. Then, when the kind also implements `ProposalCheck`, it awaits `check(project_id, payload)` and refuses with every line it returns (a tool error, before the gate). Stage 3 onboarding's calls (`Propose(kind=..., payload=...)`) keep working unchanged; the `Propose` tests that assert the two-kind error text change with it (say so in the PR).
- An allowed propose call (today only `promotion` at `bypass`, task 3's override) is applied at once by the proposals service, with no approval, and `Propose.run` returns the applied result.

**Tests (stop condition):**
- `evaluate`: each operator; `all_of`, `any_of`, `not_`; a missing field; `detail.<key>`; mixed types.
- Validation: depth 5 and 33 nodes are refused.
- Each payload refuses an extra key; each key function gives the overview's key.
- `ProposalView` validates each of the eight kinds and refuses a payload of another kind.
- `Propose` with an unregistered kind is a tool error; a registered fake kind's payload is validated by its model; a fake kind whose `check` returns a line is refused before the gate with that line.
- A fake `promotion` kind at `bypass`: the call is applied with no approval and `Propose.run` returns the applied result; at `autonomous` it asks.
- `npm test`, `npm run lint`, `npm run build` (the existing proposal card tests unchanged); `openapi_export --check`; full backend suite, ruff, format, pyright; CI green.

**PR:** early PR into `main` from `plan/stage-4-6-contracts-task4` (`Closes #<task issue>`): every proposal kind's owner starts its card on it.

---

### Task 5: Consistency tests and the regenerated API contract

**Files:** `backend/tests/test_stage4_6_contracts.py`; `frontend/openapi.json` if anything changed since task 4.

**Notes:** model the file on `tests/test_stage3_contracts.py`. These tests guard the overview's "one source" rule; they add no behaviour.

**Tests (stop condition):**
- Parity: `ResultCategory` equals `CheckSpec.category`'s literal; `ActionKind` equals `CommandSpec.kind` plus `merge_pr`.
- `diagnoser`'s targets are packaged definition names.
- Tool names in `names.py` are unique and collide with no registered tool.
- Kind names are unique and every `ProposalView` member's `kind` is one of them.
- Every event type is in `EventType` once.
- `STAT_METRICS` names are unique across groups and cover the eight `StatGroup`s; `STATS_MIN_VERDICTS == 5`.
- `ids.PREFIXES` equals the roadmap's prefix table.
- Every new payload and view model forbids extra keys (except `EvaluationPayload`).
- `openapi_export --check`; full backend suite, ruff, format, pyright; frontend checks; CI green.

---

## Known debt added

None.
