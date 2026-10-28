> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Contracts Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: the stage 3 overview (`2026-10-28-stage-3-overview.md`, in `docs/superpowers/plans/drafts/` until it moves out).

**Goal:** Land, once, what every stage 3 plan shares, so each of them only adds its own module:
- "Shared by contracts" (IDs, event types, the DuckDB constant and dependencies, project secrets, the schema, `Provider.cooldown_minutes`);
- the types of I13 to I18 (task 1), then the I19 and I20 types with the full `ConnectionsPayload` and `HandoffPayload` (task 2);
- I13 itself: the `stall` kind, the approval observers, the `propose` class, the gate's `proposal_kind` and the approval-card dispatcher.

A user sees nothing new. Every default keeps stage 2's behavior until the plan that owns it replaces the default.

**Branch:** `plan/stage-3-contracts`. **Consumes:** merged code only. **Produces:** the overview's "Shared by contracts", I13 in full, and the types of I14 to I20 (not `HookRegistry`, `ProposalKinds`, `KeyFailover`: their plans write them).

**Read first:** the stage 3 spec (`docs/superpowers/specs/2026-10-27-stage-3-onboarding-data-design.md`), §2, §9, §13 in full; the overview's interface sections; the roadmap's "Permission risk classes"; `docs/superpowers/plans/2026-10-24-stage-2-runtime-contracts.md` (this plan's model).

## Tasks

| Task | Title | Consumes | PR |
|---|---|---|---|
| 1 | Shared by contracts and the types of I13 to I18 | merged code | early PR |
| 2 | The proposal and handoff payloads (I19, I20 types) | task 1 | early PR |
| 3 | I13: stall, observers, `propose`, dispatcher | task 1 | early PR |
| 4 | Regenerated API contract and frontend types | tasks 1 to 3 | none (last task) |

Task 1 is small enough to merge within a day, because five plans start on it: IDs, events, one constant, two dependencies, the secrets module, one model field, the whole schema, and the types of I13 to I18; no behavior change. Task 2 holds the large payload models and their validators (proposals, onboarding and pages start on it). Task 3 holds I13's behavior (rate-limits' later tasks and proposals start on it). `main` must stay working after each early PR, so tasks 1 to 3 each commit a regenerated `frontend/openapi.json` for what they change (`schema.d.ts` is gitignored); task 4 closes the loop.

---

### Task 1: Shared by contracts and the types of I13 to I18

**Files:**
- `backend/src/mlops_agents/ids.py` and `tests/test_ids.py`: `con`, `trk`, `prp`, `kno`.
- `backend/src/mlops_agents/config.py`: `DUCKDB_VERSION = "2.0.0"`. `backend/pyproject.toml` and `uv.lock`: `duckdb==2.0.0` and `sqlite-vec` (`uv lock`, never by hand).
- `backend/src/mlops_agents/events/contract.py` and `frontend/src/lib/useProjectEvents.ts`: the event types.
- `backend/src/mlops_agents/credentials/project_secrets.py` (new); `backend/src/mlops_agents/workspace/service.py`: `remove` calls `project_secrets.delete_prefix(project_prefix(project_id))` (one line; `ProjectSecrets` is a constructor dependency, wired in `api/services.py`).
- `backend/src/mlops_agents/providers/models.py`: `Provider.cooldown_minutes` and `DEFAULT_COOLDOWN_MINUTES` (the only lines contracts adds there).
- `backend/src/mlops_agents/storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py` (the `approvals` rebuild is in the migration, but no Python reads its new columns until task 3).
- `backend/src/mlops_agents/agents/tools/base.py`: `_clean_schema` inlines `$defs` and `$ref`, so a tool schema sent to a provider is self-contained (the `Propose` schema has no `$ref`).
- Type modules, each new package with an empty `__init__.py`:
  - `connections/contract.py` (I15, with `LocalFolderSettings(path: str)`, `extra="forbid"`), `tracking/contract.py` (I16, with `Page[T]` and the source settings models `MlflowSettings(url: str)` and `GithubActionsSettings(repository: str, workflows: list[str] = [])`, `extra="forbid"`; `MlflowSettings.url` is checked with `base_url_problem` from `credentials/scrub.py`, so a URL with userinfo is rejected), `knowledge/contract.py` (I17);
  - `agents/hooks/contract.py` (I18: `HookContext`, `ContextHook`);
  - `providers/embedder.py` (I14: `ModelType`, `EmbedderUnavailable`, `Embedder`, `EmbedderStatus`, `Embedders`).
- `agents/runtime/contract.py`: `ApprovalKind`, `ErrorKind`, `TurnEndedData.error_kind` and `MessageView.error_kind` (null by default; the column `turns.error_kind` is the schema's; rate-limits writes and reads it).
- Regenerated `frontend/openapi.json` (the file lives in `frontend/`, written by `python -m mlops_agents.api.openapi_export`; this is the only generated file committed: `schema.d.ts` is gitignored and `npm run gen:api` rebuilds it); frontend test fixtures that stop type-checking.
- `frontend/src/lib/useProjectEvents.contract.test.ts` (new): the event-list compile check, wrapped in a `test()` (vitest fails a file with no test) and compiled by `npm run build`'s `tsc -b` (`tsconfig.app.json` includes `src`, no excludes).
- Tests: `tests/test_stage3_contracts.py`, `tests/storage/test_stage3_schema.py`, `tests/credentials/test_project_secrets.py`, and any existing test that pins a column list.

**Interfaces:** Consumes merged code. Produces the overview's "Shared by contracts" and the type parts of I13 to I18, exactly as it writes them. The overview is the one source of names and types; a needed change takes a Decision issue.

**Implementation notes:**

*Shared items.*
- Event types, backend `EventType` and frontend `PROJECT_EVENT_TYPES`: `connection.created`, `connection.updated`, `connection.deleted`, `tracking.synced`, `proposal.created`, `proposal.decided`, `handoff.approved`, `turn.stalled`, `turn.resumed`, `note.saved`, `note.deleted`. A type-level check in the contract test (`Exclude<ProjectEventType, (typeof PROJECT_EVENT_TYPES)[number]>` is `never`) makes a missing member a compile error, so the two lists cannot drift.
- `credentials/project_secrets.py`: `ProjectSecrets(keystore: KeyStore, databases: Databases)` (it uses `databases.app()`) with `put(ref, value)`, `move(src_ref, dst_ref)`, `delete(ref)`, `delete_prefix(prefix)` and `is_set(ref) -> bool`, answered from `secret_refs` and never from the keystore (slot state reads it). One instance is built in `build_services` and passed to every consumer's constructor (tracking's service, proposals, onboarding's kinds, workspace removal); nobody builds a second one. The only builders of ref strings are `proposal_slot_ref(project_id, proposal_id, slot)`, `proposal_prefix(project_id, proposal_id)`, `tracking_ref(project_id, source_id)` and `project_prefix(project_id)`; no other plan writes a ref string. It records every ref it puts in `secret_refs`, so `delete_prefix` deletes the recorded refs under the prefix without listing the keystore. `put` and `move` store first and record second; `delete` removes the secret, then the row, so a keystore failure leaves the row and a retry or the startup sweep still finds the secret. Nothing else writes project secrets; no raised message or log line contains a value.
- `Provider.cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES` (15) in `providers/models.py`; the app.db column has the same default in SQL, and a test pins the two together. Reading the column into `Provider` is rate-limits' line in `providers/service.py`.
- `DUCKDB_VERSION` is read by the backend and, later, by the sandbox image build (connections); this task adds only the constant.

*Types.*
- `CONNECTION_NAME_PATTERN` (`^[a-z0-9][a-z0-9-]{0,39}$`) is a constant in `connections/contract.py`: the one source of the connection-name rule.
- Each contract module is pure (types, protocols, constants; no I/O, no imports from implementing modules), like stage 2's, so no cycle can form.
- I13 data in this task is only `ErrorKind = Literal["keys_exhausted"]` (in `agents/runtime/contract.py`), `TurnEndedData.error_kind: ErrorKind | None = None` and `MessageView.error_kind` (the same, null by default). `ApprovalKind` and the approval fields (`kind`, `step`, `proposal_kind`, the nullable `tool`, `tool_call_id`, `risk_class`) arrive in task 3 with the frontend lines that read them, so this early PR keeps the frontend building.
- `ModelVersionRow` carries `evaluation: Literal["present", "absent", "fetch_failed"]` as the overview writes it; `EvaluationRecord`, `SyncResult.evaluations` and the `evaluations` query kind are likewise copied.
- I14 to I18: copy the overview's definitions, `Literal`s included. Member counts for the contract test: `ConnectionKind` six methods, `Connections` nine, `Tracking` four, `TrackingAdapter` one, `Knowledge` four, `VectorStore` four, `Embedders` two. `ModelType` is defined in `providers/embedder.py`; the `ModelEntry` and `ModelCreate` fields are embedders'.
*Schema.* Every `id` is the roadmap's prefixed ULID, every time UTC ISO 8601 text, JSON columns `TEXT`. These two files are the only stage 3 migrations. `storage/tables.py` declares every new table and column in the order the migration creates them (`test_tables.py` compares column order); the FTS5 virtual table and its shadow tables are not declared.

`app/0002_stage3.sql` (plain `ALTER TABLE ... ADD COLUMN`):
- `providers.cooldown_minutes INTEGER NOT NULL DEFAULT 15 CHECK (cooldown_minutes > 0)`;
- `models.model_type TEXT NOT NULL DEFAULT 'chat' CHECK (model_type IN ('chat', 'embedder'))`;
- `models.dimensions INTEGER CHECK (dimensions IS NULL OR dimensions > 0)`.
- The default embedder is a `settings` row and the project override a `project_settings` row: no schema.

`project/0004_stage3.sql`:
- `connections`: `id`, `name` UNIQUE, `kind`, `settings_json`, `credential_ref`, `status` CHECK in `ok`, `unreachable`, `status_detail`, `created`, `updated` (the overview's columns without `project_id`: `project.db` is per project and stage 2's tables carry no project column; `findings` and `data_version` are computed, never stored).
- `tracking_sources`: `id`, `type` CHECK in `mlflow`, `github_actions`, `settings` (JSON), `auth` CHECK in `none`, `credential`, `last_sync`, `last_error`, `created_at`. The credential ref is derived (`tracking_ref`), so none is stored.
- The tracking record, each table with `source_id` (FK `tracking_sources`, cascade) and a unique key of (source, ID at the source), so a sync is idempotent:
  - `tracked_models` PK (`source_id`, `name`);
  - `model_versions` PK (`source_id`, `model`, `version`), `run_key`, `aliases` (JSON), `created_at`;
  - `tracked_runs` PK (`source_id`, `key`), `experiment`, `status` CHECK in `RunStatus`, `started_at`, `ended_at`, `params`, `tags`, `git_commit`, `datasets` (the names avoid the SQL words `start` and `end`);
  - `run_metrics` PK (`source_id`, `run_key`, `metric`, `step`, `time`), `value` REAL; index on (`source_id`, `metric`, `time`);
  - `deployments` PK (`source_id`, `model`, `alias`, `seen_at`), `from_version`, `to_version` (nullable: an alias removed);
  - `ci_runs` PK (`source_id`, `key`), `workflow`, `status` CHECK in `RunStatus`, `started_at`, `duration_s`, `commit_sha` (`commit` is an SQL word), `url`;
  - `evaluations` PK (`source_id`, `model`, `version`), `run_key`, `schema_version`, `payload` (JSON), `fetched_at`.
- `notes`: `seq` INTEGER PK AUTOINCREMENT and `id` NOT NULL UNIQUE (as `messages` and `events` do, so the integer key is stable across rebuilds and VACUUM), `type` CHECK in the five types, `title`, `text`, `source`, `author`, `created_at`, `updated_at`; index on `type`. `notes_fts`: FTS5 external-content table on `notes(title, text)`, `content_rowid='seq'`, kept in step by insert, update and delete triggers.
- `note_embeddings`: `note_id` PK (FK `notes (id)`, cascade; one row per note, overwritten when the note is re-embedded), `embedder`, `dimensions`, `embedded_at` (knowledge writes it). The sqlite-vec `vec0` tables (one per dimension, rowid = `notes.seq`; knowledge maps `note_id` to `seq`) are created by the knowledge plan at run time: they need the extension loaded, which a migration cannot assume.
- `proposals`: `id`, `kind`, `key`, `payload`, `status` CHECK in the four statuses, `agent`, `approval_id` (nullable until attached; UNIQUE where not null), `turn_id`, `tool_call_id` NOT NULL, `result` (JSON, nullable), `created_at`, `decided_at`; UNIQUE (`turn_id`, `tool_call_id`) (a draft is idempotent by it); indexes on (`kind`, `key`, `status`) and on (`turn_id`, `kind`). `ProposalView.slots` is computed from the kind's `credentials_needed` and `ProjectSecrets.is_set`.
- `secret_refs` (in `app/0002_stage3.sql`, not project.db: `put(ref, value)` takes no project, the project is parsed from the ref `project/<id>/...`, and the rows must outlive a removed project, whose `project.db` `WorkspaceService.remove` deletes with its folder): `ref` PK, `project`, `created`; it holds no value. `ProjectSecrets` therefore takes the app `Databases` engine, and `delete_prefix("project/<id>/")` is what project removal can call (`WorkspaceService.remove` calls it, one line).
- `handoff_versions`: `version` INTEGER PK AUTOINCREMENT, `proposal_id` UNIQUE (one version per proposal: `Handoffs.store` is idempotent and returns the version), `approved_at`, `payload`; the current handoff is the highest version.
- `kickoff`: `handoff_version` (FK `handoff_versions`, cascade), `agent`, `message_id` (null until posted), `posted_at` (null until posted), `status` CHECK in `pending`, `posted`; PK (`handoff_version`, `agent`).
- `turns.error_kind TEXT CHECK (error_kind IS NULL OR error_kind IN ('keys_exhausted'))` (`ALTER TABLE ... ADD COLUMN`).
- `approvals`, rebuilt as `0003_approvals_tool_call.sql` did (create new, copy, drop, rename, recreate `approvals_status` and `approvals_turn_id` after the rename, since index names are global):
  - new `kind TEXT NOT NULL DEFAULT 'tool' CHECK (kind IN ('tool', 'stall'))`, `step INTEGER`, `proposal_kind TEXT`;
  - `tool_call_id`, `tool`, `risk_class` become nullable;
  - `CHECK ((kind = 'tool' AND tool_call_id IS NOT NULL AND tool IS NOT NULL AND risk_class IS NOT NULL AND step IS NULL) OR (kind = 'stall' AND tool_call_id IS NULL AND tool IS NULL AND risk_class IS NULL AND step IS NOT NULL AND proposal_kind IS NULL))`;
  - `CREATE UNIQUE INDEX approvals_stall_step ON approvals (turn_id, step) WHERE kind = 'stall' AND status = 'pending'`: unique among the pending approvals of a turn and step, so a later stall at the same step after Try again is allowed;
  - every existing row is copied with `kind = 'tool'`; foreign keys are checked after (`PRAGMA foreign_key_check` returns nothing).

**Tests (stop condition):**
- `test_stage3_contracts.py`, like stage 2's: every contract module imports with no cycle; the protocols expose the member counts above: methods are checked by name with `inspect.getmembers`, attributes of a Protocol (`name`, `payload_model`, `settings_model`, `needs_credential`, `type`) through `typing.get_type_hints`, since `getmembers` does not see annotation-only attributes.
- `CONNECTION_NAME_PATTERN` accepts `hotel-data` and `a1`, and rejects a name with a space, a capital, a leading dash or more than 40 characters.
- `_clean_schema` on a tool whose argument is a nested model (and a list of one) yields a schema with no `$ref` and no `$defs`, and the same field constraints; `LocalFolderSettings` and both tracking settings models reject an unknown field, and `MlflowSettings(url="http://user:pw@host")` is rejected; `TurnEndedData` and `MessageView` without `error_kind` validate; `Provider` defaults `cooldown_minutes` to `DEFAULT_COOLDOWN_MINUTES`, and the app.db column default equals it.
- `PROJECT_EVENT_TYPES` covers every `EventRecord` type (compile-time); `test_ids.py` accepts the four prefixes; `duckdb.__version__ == DUCKDB_VERSION` and `sqlite_vec` imports, on both platforms.
- `ProjectSecrets`, against an in-memory `KeyStore` and an app.db: `put` then `move` leaves the value under the new ref only and one `secret_refs` row; `is_set` is true after `put`, false after `delete` and never touches the keystore (a keystore whose `get` raises still answers); `delete_prefix` removes every recorded ref under the prefix and none outside it; a keystore failure on `delete` keeps the row and raises; no value appears in any raised message; removing a project (through `WorkspaceService.remove`) deletes the secrets recorded under `project_prefix(project_id)` and no other project's.
- Project schema: a project.db at version 3 with sessions, turns and approvals migrates to 4 with the same approval rows (`kind = 'tool'`), backup first (stage 1's runner); a fresh project.db gets all four; every CHECK rejects a bad value (the approvals CHECK both ways); a second pending `stall` for one turn and step is rejected, a later one after the first is decided is not; every cascade works (a tracking source removes its record rows, a note its embeddings, a handoff version its kickoff rows); the FTS table follows insert, update and delete of a note by `seq`, and still matches after `notes` is rebuilt with `INSERT ... SELECT` (the index rebuilt in that test).
- App schema: an app.db at version 1 migrates to 2, rows intact, `model_type` `chat`, `cooldown_minutes` 15; both CHECKs reject bad values.
- `test_tables.py` passes for both metadata sets. Stage 2 tests pass unchanged except those pinning column lists. `openapi_export --check` passes (the drift gate: only `frontend/openapi.json` is committed), and frontend lint, format, tests and build pass (their `pre*` scripts run `gen:api`, so the types compile against the new contract).
- Full backend suite, `ruff check`, `ruff format`, `pyright`; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task1` (five plans start on it, so it merges within a day). It `Closes` this task's issue.

---

### Task 2: The proposal and handoff payloads (I19, I20 types)

**Files:**
- New `backend/src/mlops_agents/proposals/__init__.py`, `proposals/contract.py` (I19 without `ProposalKinds`; and the I20 types), `proposals/payloads.py` (every payload model of I19).
- `backend/pyproject.toml` and `uv.lock`: `tzdata` (`uv lock`).
- Tests: `tests/proposals/test_payloads.py`, `tests/test_stage3_contracts.py` (additions for these modules).
- `frontend/openapi.json`: unchanged unless a route returns these models (none does yet); `openapi_export --check` still runs.

**Interfaces:** Consumes task 1 (`CONNECTION_NAME_PATTERN`, `SourceType`, `PermissionLevel`). Produces, exactly as the overview writes them: `ProposalStatus`, `CredentialSlot`, `SlotState`, `ApplyResult`, `ProposalView` (`payload: ConnectionsPayload | HandoffPayload`, `approval_id` optional, `tool_call_id` required, `warnings: list[str]`; typed so the cards read it from the generated schema), `CONNECTIONS_KIND = "connections"` and `HANDOFF_KIND = "handoff"` (the one source of the kind names), `ProposalKind` (seven members: `name`, `payload_model`, `preview`, `warnings`, `key`, `credentials_needed`, `apply`), `Proposals` (five methods: `create`, `attach`, `get_by_approval`, `pending`, `latest_for_turn`), `HandoffView` (with `version: int`) and `Handoffs` (`current`, `store(...) -> int`, idempotent per proposal), and every payload model. `ProposalKinds` is proposals'; `SqliteHandoffs` in `proposals/handoffs.py` is proposals'. proposals, onboarding and pages start on this task.

**Implementation notes:**
- `proposals/payloads.py` holds every model of the overview's "Payloads" block (`ConnectionSpec` to `HandoffPayload`, `GrantableLevel`, `Placeholder`), all `extra="forbid"`, imported by `proposals/contract.py` (which would pass 400 lines with them). Beyond the field lists, validators where the overview states a rule:
  - `ConnectionSpec.name` uses `CONNECTION_NAME_PATTERN` (a constant in `connections/contract.py`, task 1; the connections plan reuses it for its own validation and the `/data/<name>` mount);
  - the overview's shapes, copied exactly: `ConnectionSpec.kind` is `Literal["local_folder"]` and its `settings` is `LocalFolderSettings`; `TrackingSourceSpec.settings` is `MlflowSettings | GithubActionsSettings`; `Feed.ref` is `DataFeedRef | JobFeedRef`; `OnCall.contacts` is a list of `Contact(channel, address)`; `MlflowResults.fires_when`; `JobFeedRef` has exactly one of `ci_workflow` and `file`; each `OnCall` has one `OnCallHours` and one or more contacts;
  - `Cadence`: exactly one of `cron` and `every_minutes`; a cron is five whitespace-separated fields; `every_minutes` is at least 1;
  - every `timezone` field (`Feed`, `OnCall`, `CheckSpec`) is an IANA zone name, checked with `zoneinfo.ZoneInfo`; `tzdata` (this task's dependency) supplies the zone database that Windows lacks;
  - `OnCallHours.start` and `end` match `HH:MM`, and an `end` before `start` is a window that crosses midnight and is allowed; `end` equal to `start` is rejected;
  - `CommandSpec`: `type == "command"` needs a `template` and no `workflow`; `type == "ci_dispatch"` needs a `workflow`; every `{name}` in a template (found with `string.Formatter().parse`) is in the command's `placeholders` (declared ones that the template does not use are allowed, since `ci_dispatch` inputs use them);
  - references resolve inside the payload, as `model_validator`s that name the missing target: each `ModelSpec.predictions` and `labels` (`TableUse`) matches a `DataRole` of the payload; each `PipelineEdge` endpoint is a `PipelineStage` name; a `ModelSpec.production_signal` of kind `tracker_alias` has its `ref` among that model's `environments` keys; model names are unique. References to connections and tracking sources, which live outside the payload, are checked when the proposal is applied, not here;
  - `GrantableLevel = Literal["suggest", "ask", "autonomous"]`, so a proposal can never carry `bypass`; a test pins it to `PermissionLevel` minus `bypass`.

**Tests (stop condition):**
- Payloads: a full `HandoffPayload` (two models, feeds, on-call, all three `results` shapes, both `CommandSpec` types) round-trips through JSON; `permission_level: "bypass"` is rejected; `GrantableLevel` equals `PermissionLevel` minus `bypass`; a four-field cron and a bad `HH:MM` are rejected; a `JobFeedRef` with both or neither of `ci_workflow` and `file`, an `OnCall` with no contact, and a `ConnectionSpec` with a kind other than `local_folder` are rejected; a `ProposalView` parses a `HandoffPayload` and a `ConnectionsPayload` as the right union member; an unknown field is rejected; a connection name with a space or capital is rejected.
- Payload validators, one case each, valid and invalid: `Europe/Paris` and a Windows-safe zone pass, `Mars/Base` fails; on-call `09:00`-`17:00` and `22:00`-`06:00` pass, `09:00`-`09:00` fails; a `command` without a template, a `ci_dispatch` without a workflow, and a template using `{window_end}` without declaring it fail; a `Cadence` with both or neither field fails; a `TableUse` with no matching `DataRole`, an edge to an unknown stage, a `tracker_alias` signal whose alias is not in `environments`, and two models with one name fail, each with a message naming the missing target; an unknown field fails.
- The member counts: `ProposalKind` seven, `Proposals` five, `Handoffs` two, by the same name and `get_type_hints` rule as task 1.
- Full backend suite, `ruff check`, `ruff format`, `pyright`; `openapi_export --check`; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task2` (proposals, onboarding and pages need it). It `Closes` this task's issue.

---

### Task 3: I13: stall, observers, `propose` and the dispatcher

**Files:**
- `backend/src/mlops_agents/agents/tools/base.py` (`RiskClass.PROPOSE = "propose"`), `agents/policy/table.py`.
- `agents/runtime/contract.py`: `ApprovalKind` and the `ApprovalPart` fields (`kind`, defaulting to `"tool"` so stored stage 2 parts validate; `step`; `proposal_kind`; `tool_call_id` nullable). `agents/approvals/contract.py`: the `ApprovalView` fields (`kind`, `step`, `proposal_kind`; `tool`, `tool_call_id`, `risk_class` nullable). `agents/approvals/store.py`: `ApprovalRecord` and its row mapper read the new columns. Existing readers (`gate.py`, `service.py`, `waiting.py`, `tool_step.py`) keep type-checking because a `tool` approval always sets the fields; the pyright fixes are minimal and listed in the PR.
- `api/services.py` (`build_services`), `agents/runtime/sessions.py`, `agents/runtime/store/` (`SessionStore`), `agents/runtime/waiting.py` (`stop_waiting_turn`), `agents/runtime/tool_step.py` (`ToolStep`): the one `ApprovalStore` is built in `build_services` and passed in, never constructed elsewhere (see the notes).
- `backend/src/mlops_agents/agents/approvals/contract.py` (`Approvals` additions, `ApprovalObserver`), `store.py`, `service.py`, `decisions.py`, `gate.py` (the `proposal_kind` line).
- `backend/src/mlops_agents/agents/tools/base.py` also: `ToolContext.tool_call_id: str`; `agents/runtime/tool_step.py` (the per-call `ToolContext` builder) and the test helper `tests/agents/ctx.py` pass it; every test that builds a `ToolContext` directly passes it too: `tests/agents/messaging/test_send_message.py`, `tests/agents/messaging/test_wakeups.py`, `tests/agents/tasks/test_lifecycle.py`, `tests/agents/test_approval_gate.py`, `tests/agents/tools/test_bash.py`, `tests/agents/tools/test_list_agents.py`, `tests/api/test_messaging_routes.py` (re-run `grep -rn "ToolContext(" backend` before starting). `agents/runtime/hooks.py`: `GateDecision.proposal_kind: str | None = None`. `agents/runtime/waiting.py`: only the line that sets `proposal_kind` on the part built from a `GateDecision`; the stall's part is rate-limits' (task 3), and this plan writes no stall-part code or test there. `frontend/src/pages/agents/MessageView.tsx`: the part dispatch to the approval card (rate-limits adds Try again to the same file).
- Frontend: `frontend/src/pages/agents/ApprovalCard.tsx` (becomes the dispatcher), new `GenericApprovalCard.tsx` (the stage 2 card body and its diff, command and text views, moved unchanged) and `ApprovalControls.tsx`; `InboxPage.tsx` (it builds an `ApprovalPart` from each `ApprovalView` by hand: it must pass `kind`, `step`, `proposal_kind` and the nullable `tool_call_id`, or the stall and proposal cards never appear there) and `MessageView.tsx` only if its props change; `frontend/openapi.json` regenerated.
- Tests: `tests/agents/policy/`, `tests/agents/test_approval_store.py`, `test_approvals_service.py`, `test_approval_gate.py`, `frontend/src/pages/agents/ApprovalCard.test.tsx`, new `ApprovalControls.test.tsx`.

**Interfaces:** Consumes task 1. Produces I13 as the overview writes it:
- `Approvals.create_stall(project_id, turn_id, step, preview) -> ApprovalView`, `Approvals.cancel(project_id, approval_id) -> None`, `Approvals.add_observer(observer)`;
- `ApprovalObserver` (Protocol): `async asked(view)`, `async decided(view)`;
- the policy class `propose`; the gate's `proposal_kind`; the stall decision rules; the dispatcher; `ApprovalControls`.

**Implementation notes:**
- Policy: add the `propose` column to the table as data (the roadmap's table: `ask` at all four levels); nothing else encodes a policy value.
- Gate: when a tool's risk class is `propose`, the approval's `proposal_kind` is the call's `kind` argument (a string; anything else is left null), stored on the record and returned in the `GateDecision`; `waiting.py` copies it onto the live `ApprovalPart`. The gate tests the risk class, not a tool name, so no tool name is copied into approvals code.
- `ToolContext.tool_call_id` is set by the per-call builder in `tool_step.py` from the model's tool call ID, the same ID the gate stores on the approval, so `Proposals.create` (called from `Propose.preview` with `ctx.turn_id` and `ctx.tool_call_id`) and `attach` (found by the approval's turn and tool call) match.
- `create_stall` also records `approval.requested` (the Inbox refetches on it, `usePendingApprovals`), and a stall's decision records `approval.decided`; only the audit write is skipped.
- `create_stall` is idempotent: it inserts with `INSERT ... ON CONFLICT DO NOTHING` on the partial unique index, then reads the pending stall of that turn and step, so a re-run of the model node and two concurrent calls get one record. The row's session, agent and message are looked up from the turn (its session, the session's agent, the turn's agent message, which exists from the turn's start); a missing turn or message is a `DomainError`. `create` (tool approvals) refuses a missing tool, tool call or risk class up front, with a clear error, rather than relying on the CHECK.
- One store: `build_services` builds a single `ApprovalStore` (it holds the observers) and passes that instance to the gate, `ApprovalsService`, `SessionManager` (and through it `SessionStore`, `stop_waiting_turn` and `ToolStep`) and recovery expiry; no other code constructs one, so an observer registered once sees every transition.
- `Approvals.cancel` resumes the turn when no approval of that turn is still pending (as `decide` does).
- Observers live in `ApprovalStore`, the one place every transition passes (`create`, `create_stall`, `decide`, `cancel`, `cancel_for_turn`, `expire_non_waiting`); `ApprovalsService.add_observer` delegates to it. `asked(view)` fires after a create commits; `decided(view)` after any move out of `pending` commits (approve, reject, cancel, Stop, supersede, expiry). Observers are awaited in registration order, and the service awaits them all before it resumes or stops the turn, so a proposal is applied before the turn continues and a tool result can report what was applied. An observer that raises does not undo the decision: the error is logged (scrubbed) and the next observer still runs.
- `Approvals.cancel` marks a pending approval `cancelled`, updates its message part through `update_message`, audits it `cancelled` (tool kind), and fires `decided`; a non-pending approval gets the existing 409.
- Deciding a stall: approve calls `resume_turn` as any approval does; reject calls the session manager's `stop` for the turn's agent, which ends the turn `stopped` (Stop on a waiting turn, stage 2). Neither writes an audit record: `record_decisions` (the one place that audits, also used by cancel and expiry) skips the audit write for stall records on every path but still records `approval.decided`; rate-limits records `turn.stalled` and `turn.resumed`. `ApprovalsService.decide` copies the part with `model_copy(update=...)` instead of rebuilding it, so `kind`, `step` and `proposal_kind` survive.
- The context builder, `compaction.py` and the hub already skip `ApprovalPart` for the model; a test covers a `stall` part.
- Dispatcher. `ApprovalCard` keeps its props and exports (`ApprovalCardProps`, `ApprovalPart`, `ApprovalStatus`) and picks its component from the part alone, with no backend name copied into the frontend: `kind === "stall"` gives the `CARD_FOR_KIND.stall` entry; a non-null `proposal_kind` gives `CARD_FOR_PROPOSAL[proposal_kind]`; otherwise, or when the entry is missing, the generic card. Both tables are `Partial<Record<...>>` and empty here; rate-limits adds one line for the stall card, proposals two. A card component takes `ApprovalCardProps` plus a resolved `projectId`.
- `ApprovalControls` is the note field with the Approve and Reject buttons, the error line and the one decision POST; every card reuses it. Optional `approveLabel` and `rejectLabel` props (defaults "Approve", "Reject") let the stall card say "Try again" and "Stop".

**Tests (stop condition):**
- Policy: one test per cell of the `propose` column (4), the stage 2 table unchanged; `bypass` x `propose` is `ask`.
- Gate and waiting, one path test: a test tool of class `propose` goes through the gate and `handle_turn_waiting`; it asks at every level, `bypass` included, and the stored message part has `proposal_kind` set from the call's `kind`; a tool of another class has none. The `ToolContext` a tool receives carries the call's `tool_call_id`, and it equals the approval's.
- Through the real `build_services` wiring: a test observer registered on the service sees `asked` for a gate-created approval, and `decided` on Stop of a waiting turn and on recovery expiry; `cancel` of the last pending approval resumes its turn, and of one of two does not.
- `InboxPage` (vitest): an approval view with `kind: "stall"` and with a `proposal_kind` reaches the dispatcher with those fields.
- Store and service: `create_stall` yields a `kind='stall'` record with null tool fields; the same call twice and two concurrent calls give one record; after it is decided a new one at the same step is allowed; a missing turn is refused; `pending()`, `expire_non_waiting()` and `cancel_for_turn()` include a stall; a tool approval without a tool is refused.
- Observers: `asked` fires once per create and per stall; `decided` fires for approve, reject, cancel, `cancel_for_turn` and expiry, each once with the final view; a raising observer does not stop the next or undo the decision; every `decided` completes before the turn is resumed or stopped (a test observer that sleeps still finishes first).
- Stall decisions: approve resumes the turn and writes no audit record; reject ends the turn `stopped` with none; cancel and expiry write none; `create_stall` and each decision record `approval.requested` and `approval.decided`; a tool approval audits as before (stage 2 tests unchanged); the part keeps `kind`, `step` and `proposal_kind`.
- `ApprovalPart` stored without `kind` loads as `tool`; a `stall` part with null `tool_call_id` validates; a `stall` part round-trips through `MessageView`; the context builder and compaction skip it.
- Dispatcher (vitest): a tool approval renders the generic card; a stall renders the generic card while `CARD_FOR_KIND` is empty and a registered component when a test registers one; a part with `proposal_kind: "handoff"` renders the registered component, an unregistered kind the generic card; `ApprovalControls` uses the given labels and posts the same body as before; the stage 2 card tests pass unchanged.
- `openapi_export --check` passes; frontend lint, format, tests, build; `npm run e2e` (the approval path of the existing flow) passes. Full backend suite, ruff, format, pyright; CI green on Linux and Windows.

**PR:** open an early PR into `main` from `plan/stage-3-contracts-task3` (rate-limits' later tasks and proposals start on it). It `Closes` this task's issue.

---

### Task 4: Regenerated API contract and frontend types

**Files:** `frontend/openapi.json` (the only generated file committed; `schema.d.ts` is gitignored and rebuilt by `npm run gen:api`), `frontend/src/api/schema.contract.test.ts` (new; `expectTypeOf` checks, enforced by the build's `tsc -b`, which compiles `src`, since vitest does not typecheck without a config change), frontend test fixtures only where the final types break them.

**Interfaces:** Consumes tasks 1 to 3. Produces the committed generated contract every frontend plan builds on.

**Implementation notes:**
- Bring `main` in (`git merge origin/main`), then regenerate: `cd backend && uv run python -m mlops_agents.api.openapi_export`, then `cd frontend && npm run gen:api`. Generated files are never merged by hand.
- Models no route returns do not appear in `schema.d.ts` (I14 to I20 reach the frontend with their routes, each plan regenerating its own). What this task pins is what the contracts change on existing routes: `ApprovalPart` and `ApprovalView` (`kind`, `step`, `proposal_kind`, the nullable stall fields), the risk class value `propose`, and the event types. Frontend code reads these from `schema.d.ts` only.

**Tests (stop condition):**
- `schema.contract.test.ts` (checks wrapped in a `test()`, written against what `gen:api` emits: a field with a pydantic default may come out optional, so assert the emitted shape): `ApprovalPart["kind"]` is `"tool" | "stall"`; `ApprovalView["tool"]` and `["risk_class"]` include `null`; `ApprovalPart["proposal_kind"]` is `string | null`; the risk class union includes `"propose"`; `EventRecord["type"]` includes `"turn.stalled"`.
- `openapi_export --check` passes.
- Frontend lint, format, tests, build and `npm run e2e` pass; full backend suite, ruff, format, pyright; CI green on Linux and Windows on the plan branch commit. This is the plan's last task: it merges through the plan PR (`Closes #<plan issue>`), no early PR.

---
