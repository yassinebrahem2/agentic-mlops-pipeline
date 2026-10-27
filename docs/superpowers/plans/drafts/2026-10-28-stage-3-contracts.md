> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Contracts Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: the stage 3 overview (`2026-10-28-stage-3-overview.md`, in `docs/superpowers/plans/drafts/` until it moves out).

**Goal:** Land, once, what every stage 3 plan shares, so each of them only adds its own module:
- "Shared by contracts" (IDs, event types, the DuckDB constant and dependencies, project secrets, the schema);
- the types of I13 to I20 (the modules and protocols other plans implement);
- I13 itself: the `stall` kind, the approval observers, the `propose` class and the approval-card dispatcher.

A user sees nothing new. Every default keeps stage 2's behavior until the plan that owns it replaces the default.

**Branch:** `plan/stage-3-contracts`. **Consumes:** merged code only. **Produces:** the overview's "Shared by contracts", I13 in full, and the types of I14 to I20 (not `HookRegistry`, `ProposalKinds`, `KeyFailover`: their plans write them).

**Read first:** the stage 3 spec (`docs/superpowers/specs/2026-10-27-stage-3-onboarding-data-design.md`), §2, §9, §13 in full; the overview's interface sections; `docs/superpowers/plans/2026-10-24-stage-2-runtime-contracts.md` (this plan's model).

## Tasks

| Task | Title | Consumes | PR |
|---|---|---|---|
| 1 | Shared by contracts, and the types of I13 to I20 | merged code | early PR |
| 2 | I13: stall, observers, `propose`, dispatcher | task 1 | early PR |
| 3 | Regenerated API contract and frontend types | tasks 1 and 2 | none (last task) |

Every other plan starts on task 1 or 2 (overview, Plans table), so task 1 stays small: types, schema, IDs, events, one constant, two dependencies and the secrets module; no behavior change. `main` must stay working after each early PR, so tasks 1 and 2 each regenerate `frontend/openapi.json` and `frontend/src/api/schema.d.ts` for what they change (CI runs `openapi_export --check`); task 3 closes the loop.

---

### Task 1: Shared by contracts, and the types of I13 to I20

**Files:**
- `backend/src/mlops_agents/ids.py` and `tests/test_ids.py`: `con`, `trk`, `prp`, `kno`.
- `backend/src/mlops_agents/config.py`: `DUCKDB_VERSION = "2.0.0"`. `backend/pyproject.toml` and `uv.lock`: `duckdb==2.0.0` and `sqlite-vec` (`uv lock`, never by hand).
- `backend/src/mlops_agents/events/contract.py` and `frontend/src/lib/useProjectEvents.ts`: the event types.
- `backend/src/mlops_agents/credentials/project_secrets.py` (new).
- `backend/src/mlops_agents/storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`; `agents/approvals/store.py` (reads the new approval columns only).
- Type modules, each new package with an empty `__init__.py`:
  - `connections/contract.py` (I15), `tracking/contract.py` (I16, with `Page[T]`), `knowledge/contract.py` (I17);
  - `agents/hooks/contract.py` (I18: `HookContext`, `ContextHook`);
  - `proposals/contract.py` (I19 without `ProposalKinds`), `proposals/payloads.py`, `proposals/handoffs.py` (I20);
  - `providers/embedder.py` (I14: `ModelType`, `EmbedderUnavailable`, `Embedder`, `EmbedderStatus`, `Embedders`).
- `agents/runtime/contract.py`: `ApprovalKind`, `ErrorKind`, `ApprovalPart` fields, `TurnEndedData.error_kind`. `agents/approvals/contract.py`: `ApprovalView` fields only (the `Approvals` protocol changes in task 2, with its implementation).
- Regenerated `frontend/openapi.json` and `schema.d.ts`; frontend test fixtures that stop type-checking.
- Tests: `tests/test_stage3_contracts.py`, `tests/storage/test_stage3_schema.py`, `tests/credentials/test_project_secrets.py`, and any existing test that pins a column list.

**Interfaces:** Consumes merged code. Produces what the overview lists, exactly as it writes it. The overview is the one source of names and types; a needed change takes a Decision issue.

**Implementation notes:**

*Shared items.*
- Event types, backend `EventType` and frontend `PROJECT_EVENT_TYPES`: `connection.created`, `connection.updated`, `connection.deleted`, `tracking.synced`, `proposal.created`, `proposal.decided`, `handoff.approved`, `turn.stalled`, `turn.resumed`, `note.saved`, `note.deleted`. The frontend list is typed from the generated schema; a type-level check in the contract test makes a missing member a compile error (`Exclude<ProjectEventType, (typeof PROJECT_EVENT_TYPES)[number]>` is `never`), so the two lists cannot drift.
- `credentials/project_secrets.py`: `ProjectSecrets(keystore: KeyStore, databases: Databases)` with `put(ref, value)`, `move(src_ref, dst_ref)`, `delete(ref)`, `delete_prefix(prefix)`. Ref builders are the one place the ref formats live: `proposal_ref(project_id, prp, slot)` and `tracking_ref(project_id, trk)`. `KeyStore` has no way to list refs, so `ProjectSecrets` records every ref it writes in `secret_refs` (schema below), and `delete_prefix` deletes the listed refs under the prefix. `put` and `move` store first, record second; `delete` removes the secret, then the row; a keystore failure leaves the row, so a retry or the startup sweep still finds the secret. Nothing else writes project secrets, and no error message or log line contains a value.
- `DUCKDB_VERSION` is read by the backend and, later, by the sandbox image's build (connections); this task adds only the constant.

*Types.*
- Each contract module is pure (types, protocols, constants; no I/O, no imports from implementing modules), like stage 2's, so no cycle can form.
- I13 data: `ApprovalKind = Literal["tool", "stall"]` and `ErrorKind = Literal["keys_exhausted"]` live in `agents/runtime/contract.py`. `ApprovalPart` and `ApprovalView` gain `kind`, `step: int | None`, `proposal_kind: str | None`; for a stall `tool_call_id` is `None` on both and `tool` and `risk_class` are `None` on the view. `ApprovalPart.kind` defaults to `"tool"` so message parts stored by stage 2 still validate (stored JSON is the one place a default is needed). `TurnEndedData.error_kind: ErrorKind | None = None`. Existing readers (`gate.py`, `service.py`, `waiting.py`, `tool_step.py`, `store.py`) keep type-checking because a `tool` approval always sets the fields; the pyright fixes are minimal and listed in the PR.
- I14 to I20: copy the overview's definitions, `Literal`s included. `ConnectionKind` has the overview's seven members (`findings` included); `Connections` nine; `Tracking` four; `Knowledge` four; `VectorStore` four; `Proposals` three; `Handoffs` two. `ModelType` is defined in `providers/embedder.py`; the `ModelEntry` and `ModelCreate` fields are embedders' (`providers/models.py` is theirs).
- `proposals/payloads.py`, every model `extra="forbid"` (an agent writes them):
  - `ConnectionsPayload.connections: list[ConnectionSpec(kind, name, settings, why)]`; `name` follows `CONNECTION_NAME_PATTERN` (`^[a-z0-9][a-z0-9-]{0,39}$`), a constant in `connections/contract.py` that the connections plan reuses for its own validation and the `/data/<name>` mount.
  - `HandoffPayload`: `profile` (`objective`, `problem_type`, `target_column`, `positive_class`, `primary_metric`, `metric_rationale`, `operating_threshold`, `label_delay`; optional ones `| None`), `data_roles: list[DataRole(connection, table, role, id_column, time_column)]` (`role` in `training`, `serving_inputs`, `predictions`, `labels`, `other`), `tracking: list[TrackingSourceSpec(type: SourceType, settings, auth)]`, `pipeline: list[PipelineStage(name, entry, inputs, outputs, order)]`, `commands: Commands(train, evaluate, deploy, rollback)`, `operations: Operations(scheduler: Scheduler(name, config_location) | None, checks: list[CheckSpec(check_id, name, category, cadence, results)])`, `gaps: list[Gap(what, next_step)]`, `grants: Grants(network: bool, permission_level: GrantableLevel)`.
  - `CheckSpec.category` is `data_quality`, `drift`, `model_performance` or `operational`; `results` is a union discriminated on `kind`: `file(connection, path_pattern)`, `ci(workflow)`, `mlflow(metric_key)`.
  - `GrantableLevel = Literal["suggest", "ask", "autonomous"]`: a proposal can never carry `bypass`, and the type enforces it. A test pins it to `PermissionLevel` minus `bypass`.

*Schema.* Every `id` is the roadmap's prefixed ULID, every time UTC ISO 8601 text, JSON columns `TEXT`. These two files are the only stage 3 migrations. `storage/tables.py` declares every new table and column in the order the migration creates them (`test_tables.py` compares column order); the FTS5 virtual table and its shadow tables are not declared.

`app/0002_stage3.sql` (plain `ALTER TABLE ... ADD COLUMN`):
- `providers.cooldown_minutes INTEGER CHECK (cooldown_minutes IS NULL OR cooldown_minutes > 0)`: null means the default, which lives in one Python constant (rate-limits), so there is one source for it;
- `models.model_type TEXT NOT NULL DEFAULT 'chat' CHECK (model_type IN ('chat', 'embedder'))`;
- `models.dimensions INTEGER CHECK (dimensions IS NULL OR dimensions > 0)`.
- The default embedder is a `settings` row and the project override a `project_settings` row: no schema.

`project/0004_stage3.sql`:
- `connections`: `id`, `name` UNIQUE, `kind`, `settings` (JSON), `credential_ref`, `status` CHECK in `ok`, `unreachable`, `status_detail`, `created_at`. `findings` are computed from the files, never stored.
- `tracking_sources`: `id`, `type` CHECK in `mlflow`, `github_actions`, `settings`, `auth` CHECK in `none`, `credential`, `last_sync`, `last_error`, `created_at`. The credential ref is derived (`tracking_ref`), so none is stored.
- The tracking record, each table with `source_id` (FK `tracking_sources`, cascade) and the unique key (source, ID at the source), so a sync is idempotent:
  - `tracked_models` PK (`source_id`, `name`);
  - `model_versions` PK (`source_id`, `model`, `version`), `run_key`, `aliases` (JSON), `created_at`;
  - `tracked_runs` PK (`source_id`, `key`), `experiment`, `status` CHECK in `RunStatus`, `started_at`, `ended_at`, `params`, `tags`, `git_commit`, `datasets` (the column names avoid the SQL words `start` and `end`);
  - `run_metrics` PK (`source_id`, `run_key`, `metric`, `step`), `value` REAL, `time`; index on (`source_id`, `metric`, `time`);
  - `deployments` PK (`source_id`, `model`, `alias`, `seen_at`), `from_version`, `to_version` (nullable: an alias removed);
  - `ci_runs` PK (`source_id`, `key`), `workflow`, `status` CHECK in `RunStatus`, `started_at`, `duration_s`, `commit_sha` (`commit` is an SQL word), `url`.
- `notes`: `seq` INTEGER PK AUTOINCREMENT and `id` NOT NULL UNIQUE (as `messages` and `events` do, so the integer key is stable across rebuilds and VACUUM; knowledge uses `seq` as the vector rowid), `type` CHECK in the five types, `title`, `text`, `source`, `author`, `created_at`, `updated_at`; index on `type`. `notes_fts`: FTS5 over `title` and `text`, `content='notes'`, `content_rowid='seq'`, kept in step by insert, update and delete triggers.
- `note_embeddings`: `note_id` (FK `notes (id)`, cascade), `embedder`, `dimensions`, `embedded_at`, PK (`note_id`, `embedder`). The sqlite-vec `vec0` tables (one per dimension, rowid = `notes.seq`) are created by the knowledge plan at run time: they need the extension loaded, which a migration cannot assume.
- `proposals`: `id`, `kind`, `payload`, `status` CHECK in the four statuses, `agent`, `approval_id` UNIQUE, `turn_id`, `result` (JSON, nullable), `created_at`, `decided_at`; index on (`kind`, `status`). `ProposalView.slots` is computed from the kind's `credentials_needed` and `secret_refs`.
- `secret_refs`: `ref` PK, `created_at`; the index `ProjectSecrets` needs (see above); it holds no value.
- `handoff_versions`: `version` INTEGER PK AUTOINCREMENT, `proposal_id`, `approved_at`, `payload`; the current handoff is the highest version.
- `kickoff`: `handoff_version` (FK `handoff_versions`, cascade), `agent`, `message_id` (nullable until posted), `status` CHECK in `pending`, `posted`, `posted_at`, PK (`handoff_version`, `agent`). `project.db` is per project, so there is no project column.
- `turns.error_kind TEXT CHECK (error_kind IS NULL OR error_kind IN ('keys_exhausted'))` (`ALTER TABLE ... ADD COLUMN`).
- `approvals`, rebuilt as `0003_approvals_tool_call.sql` did (create new, copy, drop, rename, recreate `approvals_status` and `approvals_turn_id` after the rename, since index names are global):
  - new `kind TEXT NOT NULL DEFAULT 'tool' CHECK (kind IN ('tool', 'stall'))`, `step INTEGER`, `proposal_kind TEXT`;
  - `tool_call_id`, `tool`, `risk_class` become nullable;
  - `CHECK ((kind = 'tool' AND tool_call_id IS NOT NULL AND tool IS NOT NULL AND risk_class IS NOT NULL AND step IS NULL) OR (kind = 'stall' AND tool_call_id IS NULL AND tool IS NULL AND risk_class IS NULL AND step IS NOT NULL AND proposal_kind IS NULL))`;
  - `CREATE UNIQUE INDEX approvals_stall_step ON approvals (turn_id, step) WHERE kind = 'stall' AND status = 'pending'`: unique among the pending approvals of a turn and step, so a later stall at the same step after Try again is allowed;
  - every existing row is copied with `kind = 'tool'`; foreign keys are checked after (`PRAGMA foreign_key_check` returns nothing).

**Tests (stop condition):**
- `test_stage3_contracts.py`, like stage 2's: every contract module imports with no cycle; the protocols expose the overview's member names.
- Payloads: a full `HandoffPayload` round-trips through JSON; `permission_level: "bypass"` is rejected; `GrantableLevel` equals `PermissionLevel` minus `bypass`; the three `results` shapes parse and a fourth is rejected; an unknown field is rejected; a connection name with a space or capital is rejected.
- `ApprovalPart` stored without `kind` loads as `tool`; a `stall` part with null `tool_call_id` validates; `TurnEndedData` without `error_kind` validates.
- `PROJECT_EVENT_TYPES` covers every `EventRecord` type (compile-time); `test_ids.py` accepts the four prefixes; `duckdb.__version__ == DUCKDB_VERSION` and `sqlite_vec` imports, on both platforms.
- `ProjectSecrets`, against an in-memory `KeyStore` and a project.db: `put` then `move` leaves the value under the new ref only and one `secret_refs` row; `delete_prefix` removes every listed ref under the prefix and none outside it; a keystore failure on `delete` keeps the row and raises; no value appears in any raised message.
- Project schema: a project.db at version 3 with sessions, turns and approvals migrates to 4 with the same approval rows (`kind = 'tool'`), backup first (stage 1's runner); a fresh project.db gets all four; every CHECK rejects a bad value (the approvals CHECK both ways); a second pending `stall` for one turn and step is rejected, a later one after the first is decided is not; every cascade works (a tracking source removes its record rows, a note its embeddings, a handoff version its kickoff rows); the FTS table follows insert, update and delete of a note by `seq`, and still matches after `notes` is rebuilt with `INSERT ... SELECT` (the index rebuilt in that test).
- App schema: an app.db at version 1 migrates to 2, rows intact, `model_type` `chat`, `cooldown_minutes` null; both CHECKs reject bad values.
- `test_tables.py` passes for both metadata sets. Stage 2 tests pass unchanged except those pinning column lists. `npm run gen:api` leaves no diff, `openapi_export --check` passes, frontend lint, format, tests and build pass.
- Full backend suite, `ruff check`, `ruff format`, `pyright`; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task1` (every other plan starts on it). It `Closes` this task's issue.

---

### Task 2: I13: stall, observers, `propose` and the dispatcher

**Files:**
- `backend/src/mlops_agents/agents/tools/base.py` (`RiskClass.PROPOSE = "propose"`), `agents/policy/table.py`.
- `backend/src/mlops_agents/agents/approvals/contract.py` (`Approvals` additions, `ApprovalObserver`), `store.py`, `service.py`, `decisions.py`.
- `backend/src/mlops_agents/agents/runtime/store/turns.py` and the turn view: `error_kind` read and write (see Interface gaps 3).
- Frontend: `frontend/src/pages/agents/ApprovalCard.tsx` (becomes the dispatcher), new `GenericApprovalCard.tsx` (the stage 2 card body and its diff, command and text views, moved unchanged) and `ApprovalControls.tsx`; `InboxPage.tsx` and `MessageView.tsx` only if the props change; `frontend/openapi.json` and `schema.d.ts` regenerated.
- Tests: `tests/agents/policy/`, `tests/agents/test_approval_store.py`, `test_approvals_service.py`, `frontend/src/pages/agents/ApprovalCard.test.tsx`, new `ApprovalControls.test.tsx`.

**Interfaces:** Consumes task 1. Produces I13 as the overview writes it:
- `Approvals.create_stall(project_id, turn_id, step, preview) -> ApprovalView`, `Approvals.cancel(project_id, approval_id) -> None`, `Approvals.add_observer(observer)`;
- `ApprovalObserver` (Protocol): `async asked(view)`, `async decided(view)`;
- the policy class `propose`; the dispatcher; `ApprovalControls`.

**Implementation notes:**
- Policy: add the column to the table as data; nothing else encodes a policy value. `bypass` x `propose` is `ask`.
- `create_stall` is idempotent: it inserts with `INSERT ... ON CONFLICT DO NOTHING` on the partial unique index, then reads the pending stall of that turn and step, so a re-run of the model node and two concurrent calls get one record. The row's session, agent and message are looked up from the turn (its session, the session's agent, the turn's agent message, which exists from the turn's start); a missing turn or message is a `DomainError`. `create` (tool approvals) refuses a missing tool, tool call or risk class up front, with a clear error, rather than relying on the CHECK.
- Observers live in `ApprovalStore`, the one place every transition passes (the gate's `create`, `create_stall`, `decide`, `cancel`, `cancel_for_turn`, `expire_non_waiting`), and `ApprovalsService.add_observer` delegates to it. `asked(view)` fires after a create commits; `decided(view)` after any move out of `pending` commits: approve, reject, cancel (the service's `cancel`, Stop, supersede) and expiry. Observers are awaited in registration order before the call returns, so a proposal is applied before the turn resumes and its tool result can report what was applied. An observer that raises does not undo the decision: the error is logged (scrubbed) and the next observer still runs.
- `Approvals.cancel` marks a pending approval `cancelled`, updates its message part through `update_message`, audits it `cancelled` (tool kind), and fires `decided`. A non-pending approval gets the existing 409.
- A stall is not a policy decision: `record_decisions` (the one place that audits, also used by cancel and expiry) skips stall records on every path, including their `approval.decided` events; rate-limits records `turn.stalled` and `turn.resumed`. `ApprovalsService.decide` copies the part with `model_copy(update=...)` instead of rebuilding it, so `kind`, `step` and `proposal_kind` survive.
- `error_kind`: the turns store writes it when a turn ends and the turn view and `TurnEndedData` read it; nothing sets it until rate-limits does.
- The context builder, `compaction.py` and the hub already skip `ApprovalPart` for the model; a test covers a `stall` part.
- Dispatcher. `ApprovalCard` keeps its props and exports (`ApprovalCardProps`, `ApprovalPart`, `ApprovalStatus`) and picks its component from the part alone, with no backend name copied into the frontend: `kind === "stall"` → the stall entry of `CARD_FOR_KIND`; `proposal_kind` not null → `CARD_FOR_PROPOSAL[proposal_kind]`; otherwise, or when the entry is missing, the generic card. Both tables are `Partial<Record<...>>` and empty here; rate-limits adds one line for the stall card, proposals two. A card component takes `ApprovalCardProps` plus a resolved `projectId`.
- `ApprovalControls` is the note field with the Approve and Reject buttons, the error line and the one decision POST; every card reuses it. It takes optional `approveLabel` and `rejectLabel` (defaults "Approve", "Reject") so the stall card can say "Try again" and "Stop".

**Tests (stop condition):**
- Policy: one test per cell of the `propose` column (4), the stage 2 table unchanged; `bypass` x `propose` is `ask`.
- Store and service: `create_stall` yields a `kind='stall'` record with null tool fields; the same call twice and two concurrent calls give one record; after it is decided a new one at the same step is allowed; a missing turn is refused; `pending()`, `expire_non_waiting()` and `cancel_for_turn()` include a stall; a tool approval without a tool is refused.
- Observers: `asked` fires once per create and once per stall; `decided` fires for approve, reject, cancel, `cancel_for_turn`, expiry, each exactly once and with the final view; a raising observer does not stop the next one or undo the decision; `decided` completes before `decide` returns.
- Deciding, cancelling or expiring a stall writes no audit record and no `approval.decided`; a tool approval audits as before (stage 2 tests unchanged); the part keeps `kind`, `step` and `proposal_kind` after a decision.
- `error_kind` round-trips through the turns store and the turn view. A `stall` part round-trips through `MessageView`; the context builder and compaction skip it.
- Dispatcher (vitest): a tool approval renders the generic card; a stall renders the generic card while `CARD_FOR_KIND` is empty and a registered component when a test registers one; a part with `proposal_kind: "handoff"` renders the registered component, an unregistered kind the generic card; `ApprovalControls` uses the given labels and posts the same body as before; the stage 2 card tests pass unchanged.
- `npm run gen:api` leaves no diff; frontend lint, format, tests, build; `npm run e2e` (the approval path of the existing flow) passes. Full backend suite, ruff, format, pyright; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task2` (rate-limits' later tasks and proposals start on it). It `Closes` this task's issue.

---

### Task 3: Regenerated API contract and frontend types

**Files:** `frontend/openapi.json`, `frontend/src/api/schema.d.ts`, `frontend/src/api/schema.contract.test.ts` (new; `expectTypeOf` checks, enforced by the build's `tsc -b`, which compiles `src`, since vitest does not typecheck without a config change), frontend test fixtures only where the final types break them.

**Interfaces:** Consumes tasks 1 and 2. Produces the committed generated contract every frontend plan builds on.

**Implementation notes:**
- Bring `main` in (`git merge origin/main`), then regenerate: `cd backend && uv run python -m mlops_agents.api.openapi_export`, then `cd frontend && npm run gen:api`. Generated files are never merged by hand.
- Models no route returns do not appear in `schema.d.ts` (I14 to I20 reach the frontend with their routes, each plan regenerating its own). What this task pins is what the contracts change on existing routes: `ApprovalPart` and `ApprovalView` (`kind`, `step`, `proposal_kind`, the nullable stall fields), the risk class value `propose`, and the event types. Frontend code reads these from `schema.d.ts` only.

**Tests (stop condition):**
- `schema.contract.test.ts`: `ApprovalPart["kind"]` is `"tool" | "stall"`; `ApprovalView["tool"]` and `["risk_class"]` include `null`; `ApprovalPart["proposal_kind"]` is `string | null`; the risk class union includes `"propose"`; `EventRecord["type"]` includes `"turn.stalled"`.
- `openapi_export --check` passes and `npm run gen:api` leaves no diff.
- Frontend lint, format, tests, build and `npm run e2e` pass; full backend suite, ruff, format, pyright; CI green on Linux and Windows on the plan branch commit. This is the plan's last task: it merges through the plan PR (`Closes #<plan issue>`), no early PR.

---

## Interface gaps

Still open; each was decided here so work can proceed, and takes a Decision issue to change:

1. **`ProjectSecrets.delete_prefix` and `KeyStore`.** `KeyStore` has `get`, `set`, `delete` only; the keyring backend cannot list. Decided here: a `secret_refs` table (not in the overview's schema list) recording every ref `ProjectSecrets` writes, which also serves the startup sweep of orphaned slots. Confirm, or name another source for the list.
2. **Who sets `approval.proposal_kind`.** The gate creates the approval from the tool call, and `gate.py` is in no plan's file list. The `Propose` tool must hand its kind to the gate (for example an optional `AgentTool` method the gate calls, passed to `ApprovalStore.create`). Needs an owner (proposals' task 1 for the tool method; the gate edit is one line in `approvals/gate.py`).
3. **`turns.error_kind` plumbing.** The overview names the field and the turn view but no owner for the turns store. This plan writes the store read and write in task 2 and rate-limits sets it; confirm.
4. **`providers/models.py` ownership.** It is embedders' (`ModelEntry`, `ModelCreate`), but rate-limits needs a `Provider.cooldown_minutes` field there (the column is in task 1, null = the default constant). One of the two plans must add it; the overview should say which.
5. **`ApprovalControls` props and observer semantics.** The overview names the component and the observer methods, not their details. Decided here: `approveLabel` and `rejectLabel` props; observers awaited in order before the decision call returns, errors logged and not propagated. Confirm.
6. **Path.** The overview puts the stall's waiting code in `agents/approvals/waiting.py`; in the code it is `agents/runtime/waiting.py`. This plan touches neither.
