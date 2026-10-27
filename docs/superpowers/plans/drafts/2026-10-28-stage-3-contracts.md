> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Contracts Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: the stage 3 overview (`2026-10-28-stage-3-overview.md`, in `docs/superpowers/plans/drafts/` until it moves out).

**Goal:** Land, once, what every stage 3 plan shares, so each of them only adds its own module:
- the types and protocols of I13 to I20;
- the whole stage 3 schema;
- the `propose` risk class, the `stall` approval kind, and the approval-card dispatcher.

A user sees nothing new. Every default keeps stage 2's behavior until the plan that owns it replaces the default.

**Branch:** `plan/stage-3-contracts`. **Consumes:** merged code only. **Produces:** I13 to I20 in code (I18 without `HookRegistry`, see Interface gaps), `app/0002_stage3.sql`, `project/0004_stage3.sql` with `tables.py`, the `propose` class, the `stall` kind, the dispatcher.

**Read first:** the stage 3 spec (`docs/superpowers/specs/2026-10-27-stage-3-onboarding-data-design.md`), §2, §9, §13 in full; the overview's interface section; `docs/superpowers/plans/2026-10-24-stage-2-runtime-contracts.md` (this plan's model).

## Tasks

| Task | Title | Consumes | PR |
|---|---|---|---|
| 1 | Types, protocols and the stage 3 schema | merged code | early PR |
| 2 | `propose`, `stall` and the approval-card dispatcher | task 1 | early PR |
| 3 | Regenerated API contract and frontend types | tasks 1 and 2 | none (last task) |

Every other plan starts on task 1 or task 2 (overview, Plans table). Because `main` must stay working after each early PR, tasks 1 and 2 each regenerate `frontend/openapi.json` and `frontend/src/api/schema.d.ts` for what they change (CI runs `openapi_export --check`); task 3 closes the loop.

---

### Task 1: Types, protocols and the stage 3 schema

**Files:**
- New packages, each `__init__.py` empty plus the named module:
  - `backend/src/mlops_agents/connections/contract.py` (I15);
  - `backend/src/mlops_agents/tracking/contract.py` (I16);
  - `backend/src/mlops_agents/knowledge/contract.py` (I17);
  - `backend/src/mlops_agents/agents/hooks/contract.py` (I18);
  - `backend/src/mlops_agents/proposals/contract.py` (I19), `proposals/payloads.py` (`ConnectionsPayload`, `HandoffPayload` and their parts), `proposals/handoffs.py` (I20);
  - `backend/src/mlops_agents/providers/embedder.py` (I14's `Embedder`, `Embedders`, `EmbedderUnavailable`).
- `backend/src/mlops_agents/providers/models.py`: `ModelType`, `ModelCreate.model_type`, `ModelEntry.model_type` and `dimensions`. `providers/service.py`: the row mapping (the `ModelEntry` builder near line 366) reads both columns and `add_model` stores `model_type`.
- `backend/src/mlops_agents/agents/runtime/contract.py`: `ApprovalKind`, `ApprovalPart.kind` and nullable `ApprovalPart.tool_call_id`.
- `backend/src/mlops_agents/agents/approvals/contract.py`: `ApprovalView.kind`, and `tool_call_id`, `tool`, `risk_class` nullable.
- `backend/src/mlops_agents/events/contract.py`: the new `EventType` values.
- `backend/src/mlops_agents/ids.py` (prefixes `con`, `trk`, `prp`, `kno`; already in the roadmap) and its test.
- `backend/src/mlops_agents/storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`.
- `backend/src/mlops_agents/agents/approvals/store.py`: `_row_to_record` and `ApprovalRecord` read the new columns and the `kind` default; no stall behavior yet.
- `frontend/openapi.json`, `frontend/src/api/schema.d.ts` regenerated, and the frontend fixtures that no longer type-check (test files only, for the new `ApprovalPart.kind`, `ModelEntry.model_type` and `dimensions`).
- Tests: `backend/tests/test_stage3_contracts.py`, `backend/tests/storage/test_stage3_schema.py`, `backend/tests/test_ids.py`, plus any existing test that pins the approvals or models column list.

**Interfaces:** Consumes merged code. Produces I13 (types only: the `kind` fields, the event names), I14 to I20 exactly as the overview writes them, and the schema below. The overview is the one source of names and types; this task writes them into code unchanged. A needed change takes a Decision issue. Where the overview leaves a shape open, this plan decides it and the Interface gaps section says so.

**Implementation notes:**

*Types.*
- Each contract module is pure (types, protocols, constants; no I/O, no imports from implementing modules), like stage 2's contract modules, so no cycle can form.
- `ApprovalKind = Literal["tool", "stall"]` lives in `agents/runtime/contract.py` beside `ApprovalStatus`; `approvals/contract.py` imports it. `ApprovalPart.kind` defaults to `"tool"` so message parts stored by stage 2 still validate; this is the one place the default is needed (stored JSON), so it stays in the model.
- `ApprovalView` and `ApprovalPart` become nullable for what a stall lacks: `tool_call_id: str | None`, and on the view `tool: str | None`, `risk_class: RiskClass | None`. Existing code that reads them (`gate.py`, `service.py`, `waiting.py`, `tool_step.py`, `store.py`, `list_for_step(tool_call_ids=...)`) keeps type-checking because a `tool` approval always sets them; the pyright fixes are minimal and listed in the PR.
- `EventType` gains: `turn.stalled`, `turn.resumed`, `connection.created`, `connection.deleted`, `tracking.synced`, `proposal.created`, `proposal.decided`, `handoff.approved` (I13, I15, I16, I19).
- I14: `ModelType = Literal["chat", "embedder"]`. `Embedder` and `Embedders` are `Protocol`s exactly as written; `EmbedderUnavailable` is an `Exception` whose message is user-facing.
- I15 to I20: copy the overview's classes. `TableRef.format`, `SourceType`, `NoteType` and the others are the `Literal`s as written. `ConnectionKind` is exactly the spec §4 interface (`name`, `settings_model`, `needs_credential`, `validate`, `tables`, `data_version`, `duckdb_source`, `sandbox_mount`), written as a `Protocol`.
- I18: `HookContext` and the `ContextHook` protocol only; `HookRegistry` is a class with behavior and belongs to data-summary task 1.
- `proposals/payloads.py`, every model `extra="forbid"` (an agent writes them):
  - `ConnectionsPayload.connections: list[ConnectionSpec]`, `ConnectionSpec(kind: str, name: str, settings: dict[str, Any], why: str)`; `name` uses `CONNECTION_NAME_PATTERN` (`^[a-z0-9][a-z0-9-]{0,39}$`), a constant in `connections/contract.py` that the connections plan reuses for validation and the `/data/<name>` mount.
  - `HandoffPayload`: `profile` (`objective`, `problem_type`, `target_column`, `positive_class`, `primary_metric`, `metric_rationale`, `operating_threshold`, `label_delay`; the optional ones `| None`), `data_roles: list[DataRole(connection, table, role, id_column, time_column)]` with `role: Literal["training", "serving_inputs", "predictions", "labels", "other"]`, `tracking: list[TrackingSourceSpec(type: SourceType, settings, auth)]`, `pipeline: list[PipelineStage(name, entry, inputs, outputs, order)]`, `commands: Commands(train, evaluate, deploy, rollback; each str | None)`, `operations: Operations(scheduler: Scheduler(name, config_location) | None, checks: list[CheckSpec(check_id, name, category, cadence, results)])`, `gaps: list[Gap(what, next_step)]`, `grants: Grants(network: bool, permission_level: GrantableLevel)`.
  - `CheckSpec.category` is `Literal["data_quality", "drift", "model_performance", "operational"]`; `results` is a union discriminated on `kind`: `file(connection, path_pattern)`, `ci(workflow)`, `mlflow(metric_key)`.
  - `GrantableLevel = Literal["suggest", "ask", "autonomous"]` is defined here: a proposal can never carry `bypass`, and the type is what enforces it. A test pins it to `PermissionLevel` minus `bypass`, so adding a level cannot silently widen it.

*Schema.* Every `id` is the roadmap's prefixed ULID, every time UTC ISO 8601 text, JSON columns are `TEXT`. Both files are the only stage 3 migrations; later plans add none without a Decision issue.

`app/0002_stage3.sql` (plain `ALTER TABLE ... ADD COLUMN`; SQLite allows a CHECK with a default):
- `providers.cooldown_minutes INTEGER NOT NULL DEFAULT 15 CHECK (cooldown_minutes > 0)`;
- `models.model_type TEXT NOT NULL DEFAULT 'chat' CHECK (model_type IN ('chat', 'embedder'))`;
- `models.dimensions INTEGER CHECK (dimensions IS NULL OR dimensions > 0)`.
- The default embedder is a row in `settings` (key/value) and the project override one in `project_settings`: no schema.

`project/0004_stage3.sql`:
- `connections`: `id`, `name` UNIQUE, `kind`, `settings` (JSON), `credential_ref` (nullable), `status` CHECK in `ok`, `unreachable`, `status_detail`, `created_at`.
- `tracking_sources`: `id`, `type` CHECK in `mlflow`, `github_actions`, `settings` (JSON), `auth` CHECK in `none`, `credential`, `last_sync_at`, `last_error`, `created_at`. The credential ref is derived (`project/<id>/tracking/<trk>`), so none is stored.
- The tracking record, every table keyed by `source_id` (FK `tracking_sources`, cascade) and the source's own ID, so a sync is idempotent:
  - `tracked_models` PK (`source_id`, `name`);
  - `model_versions` PK (`source_id`, `model`, `version`), `run_key`, `aliases` (JSON), `created_at`;
  - `tracked_runs` PK (`source_id`, `key`), `experiment`, `status`, `started_at`, `ended_at`, `params` (JSON), `tags` (JSON), `git_commit`, `datasets` (JSON); the column names avoid the SQL words `start` and `end` (`RunRow.start` maps to `started_at`);
  - `run_metrics` PK (`source_id`, `run_key`, `metric`, `step`), `value` REAL, `time`; index on (`source_id`, `metric`, `time`);
  - `deployments` PK (`source_id`, `model`, `alias`, `seen_at`), `from_version`, `to_version`;
  - `ci_runs` PK (`source_id`, `key`), `workflow`, `status`, `conclusion`, `started_at`, `duration_s`, `commit_sha` (`commit` is an SQL word; `CiRunRow.commit` maps to it), `url`.
- `notes`: `seq` INTEGER PK AUTOINCREMENT, `id` NOT NULL UNIQUE (as `messages` and `events` do, so the rowid is stable across table rebuilds and VACUUM), `type` CHECK in the five note types, `title`, `text`, `source`, `author`, `created_at`, `updated_at`; index on `type`. `notes_fts`: FTS5 over `title` and `text` with `content='notes'` and `content_rowid='seq'`, kept in step by insert, update and delete triggers.
- `note_embeddings`: `vec_id` INTEGER PK AUTOINCREMENT, `note_id` (FK `notes (id)`, cascade), `embedder`, `dimensions`, `embedded_at`, UNIQUE (`note_id`, `embedder`). The vectors live in sqlite-vec `vec0` tables, one per dimension, keyed by `vec_id`; the knowledge plan creates them at run time (they need the extension loaded, which a migration cannot assume). The migration holds only what is plain SQL.
- `proposals`: `id`, `kind`, `payload` (JSON), `status` CHECK in `pending`, `approved`, `rejected`, `superseded`, `agent`, `approval_id`, `version` INTEGER, `apply_result` (JSON, nullable), `created_at`, `decided_at`; index on (`kind`, `status`). `proposal_credentials`: `proposal_id` (FK, cascade), `slot` INTEGER, `set_at`, PK (`proposal_id`, `slot`); it holds only that a credential is set, never the value.
- `handoffs`: `seq` INTEGER PK AUTOINCREMENT, `proposal_id`, `approved_at`, `payload` (JSON); the current handoff is the highest `seq`.
- `kickoffs`: `proposal_id`, `agent`, `message_id`, `posted_at`, PK (`proposal_id`, `agent`): one row per audit agent per handoff, so a restart neither repeats nor loses one.
- `approvals`, rebuilt as `0003_approvals_tool_call.sql` did (create new, copy, drop, rename, recreate `approvals_status` and `approvals_turn_id` after the rename, since index names are global):
  - new `kind TEXT NOT NULL DEFAULT 'tool' CHECK (kind IN ('tool', 'stall'))` and `step INTEGER`;
  - `tool_call_id`, `tool`, `risk_class` become nullable;
  - `CHECK ((kind = 'tool' AND tool_call_id IS NOT NULL AND tool IS NOT NULL AND risk_class IS NOT NULL AND step IS NULL) OR (kind = 'stall' AND tool_call_id IS NULL AND step IS NOT NULL))`;
  - `CREATE UNIQUE INDEX approvals_stall_step ON approvals (turn_id, step) WHERE kind = 'stall'`: the one key that makes a re-run of the model node find its stall instead of adding another;
  - every existing row is copied with `kind = 'tool'`, nothing else changed. Foreign keys are checked after (`PRAGMA foreign_key_check` returns nothing).
- `storage/tables.py` declares every new table and column in the order the migration creates them (`test_tables.py` compares column order). The FTS5 virtual table and its shadow tables are not declared.

**Tests (stop condition):**
- `test_stage3_contracts.py`, like stage 2's: every contract module imports with no cycle; the protocols expose the member names of I14 to I20 (`ConnectionKind` the spec's eight, `Connections` seven, `Tracking` four, `Knowledge` three, `VectorStore` three, `Proposals` three, `Handoffs` two).
- Payloads: a full `HandoffPayload` round-trips through JSON; `permission_level: "bypass"` is rejected; `GrantableLevel` equals `PermissionLevel` minus `bypass`; the three `results` shapes parse and a fourth is rejected; an unknown field is rejected; a connection name with a space or capital is rejected (`CONNECTION_NAME_PATTERN` is the one source of the rule).
- `ApprovalPart` stored without `kind` (stage 2 JSON) loads as `tool`; a `stall` part with a null `tool_call_id` validates; `ModelCreate` defaults to `chat`; `ModelEntry` rows from stage 2 data read `chat` and `dimensions` null.
- Project schema: a project.db at version 3 with sessions, turns and approvals migrates to 4 with the same approval rows, `kind = 'tool'`, backup taken first (stage 1's runner); a fresh project.db gets all four; every CHECK rejects a bad value (including the approvals CHECK both ways); a second `stall` row for the same turn and step is rejected, a different step or a `tool` row is not; every cascade works (deleting a tracking source removes its record rows, a note its embeddings, a proposal its credential rows); the FTS table follows insert, update and delete of a note by `seq`, and still matches after the `notes` table is rebuilt with `INSERT ... SELECT` (the index is rebuilt in that test).
- App schema: an app.db at version 1 with providers and models migrates to 2, rows intact, `cooldown_minutes` 15 and `model_type` `chat`; both CHECKs reject bad values. `15` is written in SQL once; rate-limits pins its Python cooldown constant to it (Interface gaps 7).
- `test_tables.py` passes for both metadata sets; `test_ids.py` accepts the four new prefixes.
- Stage 2 tests pass unchanged except those pinning column lists. `npm run gen:api` leaves no diff, `openapi_export --check` passes, frontend lint, format, tests and build pass.
- Full backend suite, `ruff check`, `ruff format`, `pyright`; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task1` (every other plan starts on it). It `Closes` this task's issue.

---

### Task 2: `propose`, `stall` and the approval-card dispatcher

**Files:**
- `backend/src/mlops_agents/agents/tools/base.py`: `RiskClass.PROPOSE = "propose"`.
- `backend/src/mlops_agents/agents/policy/table.py`: the `propose` cell of every level.
- `backend/src/mlops_agents/agents/approvals/store.py`: stall create and lookup.
- `backend/src/mlops_agents/agents/approvals/contract.py`: `STALL_TITLE` and the stall preview and part builders.
- `backend/src/mlops_agents/agents/approvals/decisions.py` (`record_decisions`) and `service.py` (`decide`): stall rules.
- `backend/src/mlops_agents/agents/runtime/waiting.py`: only if it must build a stall part; see notes.
- Frontend: `frontend/src/pages/agents/ApprovalCard.tsx` (becomes the dispatcher), new `GenericApprovalCard.tsx` (the stage 2 card body and its diff, command and text views, moved unchanged) and `ApprovalActions.tsx` (note field, decision buttons, error, with overridable labels); `InboxPage.tsx` and `MessageView.tsx` pass what the dispatcher needs; `frontend/openapi.json` and `schema.d.ts` regenerated.
- Tests: `backend/tests/agents/policy/`, `backend/tests/agents/test_approval_store.py` and `test_approvals_service.py` (additions), `frontend/src/pages/agents/ApprovalCard.test.tsx` (extended), new `ApprovalActions.test.tsx`.

**Interfaces:** Consumes task 1. Produces the rest of I13: the class `propose` (`ask` at all four levels), the `stall` approval record, `stall_preview` and `stall_part` (below), and the dispatcher's two tables.

**Implementation notes:**
- Policy: add the column to the table as data; nothing else in the code base encodes a policy value. `bypass` x `propose` is `ask`, the one `ask` in that row.
- Store: `ApprovalStore.create_stall(project_id, *, turn_id, session_id, message_id, agent, step, preview) -> ApprovalRecord` inserts with `kind = 'stall'`, null `tool_call_id`, `tool`, `risk_class`, and returns the existing row when one exists for (`turn_id`, `step`): an `INSERT ... ON CONFLICT DO NOTHING` on the partial unique index, then a read, so two concurrent re-runs get the same record. `get_stall(project_id, turn_id, step)` reads it. `create` rejects a call with `kind='tool'` and a missing tool field up front, with a clear error, rather than relying on the CHECK.
- Contract helpers, one source for the stall's text: `STALL_TITLE = "Stalled: no usable key"`; `stall_preview(body: str) -> Preview` (kind `text`, that title, the body the rate-limits plan renders from its per-key states); `stall_part(record: ApprovalView) -> ApprovalPart` (kind `stall`, null `tool_call_id`). The rate-limits plan calls these; it never builds the part itself.
- `record_decisions` (the one place that audits a decision, also used by cancel and expiry) skips records whose kind is `stall`: a stall is not a policy decision and writes no audit record, on every path, Stop and recovery included. `ApprovalsService.decide` copies the part with `model_copy(update=...)` instead of rebuilding it, so `kind` survives, and for a stall also writes no `approval.decided` event (rate-limits records `turn.resumed`). Clearing the key marks and resuming are rate-limits'; this task only guarantees the generic path handles a stall record without an audit write and without error.
- The context builder, `compaction.py` and the hub already skip `ApprovalPart` for the model; a test covers a `stall` part.
- Dispatcher. `ApprovalCard` keeps its props and exports (`ApprovalCardProps`, `ApprovalPart`, `ApprovalStatus`) and gains an optional `toolInput?: Record<string, unknown>`. It picks the component:
  - `kind === "stall"` → `CARD_FOR_KIND.stall`, else the generic card;
  - `kind === "tool"` with `toolName === "Propose"` → `CARD_FOR_PROPOSAL[String(toolInput?.kind)]`, else the generic card;
  - anything else → the generic card.

  `CARD_FOR_KIND: Partial<Record<ApprovalKind, CardComponent>>` and `CARD_FOR_PROPOSAL: Partial<Record<string, CardComponent>>` are empty tables in this task; rate-limits adds one entry for the stall card, proposals two (`connections`, `handoff`). `CardComponent` takes the props of `ApprovalCardProps` plus a resolved `projectId: string`. `ApprovalActions` takes `approveLabel` and `rejectLabel` (defaults "Approve" and "Reject") so the stall card can say "Try again" and "Stop" without a second decision form; the decision POST stays in one place.
- `InboxPage.tsx` and `MessageView.tsx` pass the matching tool call's `input` as `toolInput`; a stall has no matching tool call and the header falls back to "Approval required".
- Regenerate `openapi.json` and `schema.d.ts`; the new `propose` value and the nullable fields reach the frontend types.

**Tests (stop condition):**
- Policy: one test per cell of the new column (4), plus the existing 24 unchanged; `bypass` x `propose` is `ask`.
- Store: `create_stall` creates a record readable as `kind='stall'`; the same call twice (and two concurrent calls) returns one record; a different step creates another; `pending()` and `expire_non_waiting()` and `cancel_for_turn()` include a stall like any approval; `create(kind='tool')` without a tool is refused.
- Service: deciding a stall approval updates its part (kind preserved), writes no audit record and no `approval.decided`; cancel and expiry of a stall write no audit record; a tool approval still audits as before (stage 2 tests unchanged).
- A `stall` part round-trips through `MessageView`; the context builder and compaction skip it.
- Dispatcher (vitest): a tool approval renders the generic card; a stall approval renders the generic card while `CARD_FOR_KIND` is empty and the registered component when a test registers one; a `Propose` approval with `kind: "handoff"` renders the registered proposal component, an unknown proposal kind the generic card; `ApprovalActions` uses the given labels and posts the same body as before; the stage 2 card tests pass unchanged.
- `npm run gen:api` leaves no diff, frontend lint, format, tests, build; `npm run e2e` (the approval card path of the existing flow) passes.
- Full backend suite, ruff, format, pyright; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task2` (rate-limits and proposals start on it). It `Closes` this task's issue.

---

### Task 3: Regenerated API contract and frontend types

**Files:** `frontend/openapi.json`, `frontend/src/api/schema.d.ts`, `frontend/src/api/schema.contract.test.ts` (new; `expectTypeOf` checks, enforced by the build's `tsc -b`, which compiles `src`, since vitest does not typecheck without a config change), frontend test fixtures only where the final types break them.

**Interfaces:** Consumes tasks 1 and 2. Produces the committed generated contract every frontend plan builds on.

**Implementation notes:**
- Bring `main` in (`git merge origin/main`), then regenerate: `cd backend && uv run python -m mlops_agents.api.openapi_export`, then `cd frontend && npm run gen:api`. Generated files are never merged by hand.
- Models no route returns do not appear in `schema.d.ts` (I15 to I20 reach the frontend with their routes, each plan's own regeneration). What this task pins is what the stage 3 contracts already change on existing routes: `ApprovalPart` and `ApprovalView` (`kind`, nullable `tool_call_id`, `tool`, `risk_class`), the risk class value `propose`, `ModelCreate.model_type`, `ModelEntry.model_type` and `dimensions`.
- No hand-written copy of any of those types: frontend code reads them from `schema.d.ts` only (overview, Global constraints).

**Tests (stop condition):**
- `schema.contract.test.ts` (`expectTypeOf`, compiled by `npm run build`): `ApprovalPart["kind"]` is `"tool" | "stall"`; `ApprovalView["tool"]` and `["risk_class"]` include `null`; `ModelEntry["model_type"]` is `"chat" | "embedder"`; the risk class union includes `"propose"`.
- `openapi_export --check` passes and `npm run gen:api` leaves no diff.
- Frontend lint, format, tests, build and `npm run e2e` pass; full backend suite, ruff, format, pyright; CI green on Linux and Windows on the plan branch commit. This is the plan's last task: it merges through the plan PR (`Closes #<plan issue>`), no early PR.

---

## Interface gaps

Needed from the lead (nothing below was invented in the tasks above except where marked "decided here"; each takes a Decision issue to change):

1. **I13, what a stall lacks (decided here).** The overview makes only `tool_call_id` null. A stall also has no tool and no risk class, and keying by (`turn_id`, `step`) needs a `step` column. Decided here: `tool` and `risk_class` nullable on the record and on `ApprovalView`, a `step` column, the table CHECK and the partial unique index of task 1. `ApprovalPart.kind` defaults to `tool` for stored stage 2 parts.
2. **I13/I15 to I19, events (decided here).** The overview lists only `turn.stalled` and `turn.resumed` as new event types, but I15, I16 and I19 name six more; all eight join `EventType` in task 1.
3. **I18, `HookRegistry`.** The overview lists it in `agents/hooks/contract.py` but the Plans table makes data-summary task 1 its producer. This plan writes `HookContext` and `ContextHook` only.
4. **I19, `credentials/project_secrets.py`.** It is named as the one place credentials are written, used by the proposal slot route and the tracking credential route, but no plan's row produces it. Needs an owner (contracts task 1 or proposals task 1); tracking needs it before proposals does.
5. **I19, the proposal card.** The handoff card must PUT a credential to `.../proposals/{prp}/credentials/{slot}`, so it needs the proposal ID, and the dispatcher only has the approval ID and the `Propose` call's input. Needs a lookup (for example a route returning the `ProposalView` for an `approval_id`) in proposals' routes; the dispatcher here reads the proposal kind from the tool call's `kind` argument. That hard-codes the backend names `Propose`, `connections` and `handoff` in TypeScript, against "generate it, never copy it"; the clean fix is for the approval or its part to carry the proposal kind, an I13 change that takes a Decision.
6. **Names this plan adds that other plans call (the overview should gain them).** `ApprovalStore.create_stall`, `get_stall`, `STALL_TITLE`, `stall_preview`, `stall_part` (rate-limits); `CARD_FOR_KIND`, `CARD_FOR_PROPOSAL`, `CardComponent` and `ApprovalActions`' `approveLabel` and `rejectLabel` (rate-limits, proposals); `CONNECTION_NAME_PATTERN` (connections); the `HandoffPayload` and `ConnectionSpec` field names of task 1 (proposals, onboarding); the `note_embeddings.vec_id` key (knowledge).
7. **I21 names `ProviderView`**, which does not exist (the code has `Provider`); rate-limits' concern. The `providers.cooldown_minutes` column is in task 1; the `Provider` field, its route and a Python cooldown constant pinned to that default by a test belong to rate-limits.
8. **I14, `ModelEntry.problem`.** The per-type usable rule is embedders'. Until it lands, an `embedder` entry verifies as a chat model and fails, so the UI must not offer the type before then.
