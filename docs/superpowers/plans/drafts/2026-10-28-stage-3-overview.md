# Stage 3: Onboarding and Data Access — Plans Overview

> **Draft.** Missing: an advisor round with no blocking issues; the plans it lists.

Stage 3 is built through several plans. This overview holds what they share. Read it, `docs/roadmap.md` and the stage 3 spec before starting any plan.

**Goal:** after one conversation with Onboarding and two approvals, the app knows a project's data, tracking, pipeline, operations and existing monitoring; the four specialist agents start their first audits. Turns survive rate limits.

**Spec:** `docs/superpowers/specs/2026-10-27-stage-3-onboarding-data-design.md` (approved 2026-10-28). Section numbers (§n) below are the spec's.

**Tech stack:** as in stage 2, plus DuckDB 2.0.0 (backend and the sandbox image, one version constant) and sqlite-vec.

## Global constraints

- The rules of `AGENTS.md` apply in full; the spec's principles too (an extension of the team's system; Bash unless a tool is justified; no whole datasets in context; secrets never in a proposal, a log, an event or a checkpoint).
- Every external service is faked in tests: the fake OpenAI server (`/embeddings`, 429, 402), a new fake MLflow REST server, the fake GitHub server (workflow runs). No test needs a real provider, tracker or Docker unless marked as the stage 2 Docker tests are.
- One source for every constant: the DuckDB version, the hook caps, the cache cap, the cooldown default, the hook names, the proposal kinds.
- Tool schemas sent to providers are self-contained: `agents/tools/base.py`'s `_clean_schema` inlines `$defs` (contracts task 1, with a test on a nested model), so the `Propose` schema has no `$ref`.
- The generated contract check is `python -m mlops_agents.api.openapi_export --check`; `npm run gen:api` only writes `schema.d.ts`.
- Frontend types come only from the generated `schema.d.ts` (`npm run gen:api`), which is gitignored and never committed; a plan that changes the API commits the regenerated `frontend/openapi.json`.
- Routers are discovered by `include_routers` in `api/app.py`; a plan adds its router file and no registration line.

## Plans

| Plan | Branch | Covers | Consumes (besides merged code) | Produces | Starts | State |
|---|---|---|---|---|---|---|
| contracts (claim first) | `plan/stage-3-contracts` | I13 to I20 as models and protocols; the stage 3 schema; the `propose` class; the `stall` kind in the approvals store; the approval-card dispatcher | none | I13 to I20 in code, both migrations, the dispatcher | now | Planned |
| rate-limits | `plan/stage-3-rate-limits` | §2: the key failover engine (per key and model), the error classification, the stall (model node, graph routing, card, Try again), the cooldown setting | task 1 none; the rest I13 | `KeyFailover` (I21) from task 1 (early PR); the stall | task 1 now; the rest after contracts task 3 | Planned |
| embedders | `plan/stage-3-embedders` | §3: model types, the embedder verify step, type-filtered pickers, the embedder settings, the fake `/embeddings`, the `Embedders` service | I14; I21 | `Embedders` (I14) from task 1 (early PR) | after contracts task 1 and rate-limits task 1 | Planned |
| connections | `plan/stage-3-connections` | §4, §5: the registry and its routes, the `local_folder` kind, the settings section, read-only mounts and the container fingerprint, DuckDB in the environment image, the host-loopback test | I15 | `Connections` (I15), its routes and `ConnectionFindings` from task 1 (early PR) | after contracts task 1 | Planned |
| data-summary | `plan/stage-3-data-summary` | §6: the hook registry and the one injection point, `status_board` as a hook, `data_summary` with the DuckDB lock-down, the cache, the table-detail route | I15, I18 | `HookRegistry` (I18) from task 1 (early PR); `GET .../connections/{con}/tables/{table}` | task 1 after contracts task 1; the rest also after connections task 1 | Planned |
| tracking | `plan/stage-3-tracking` | §7: sources and routes, the MLflow and GitHub Actions adapters, the fake MLflow server, the sync loop, `TrackingHistory`, `tracking_summary` | I16, I18 | `Tracking` (I16) and its routes from task 1 (early PR) | tasks 1 to 4 after contracts task 1; task 5 after contracts task 2, data-summary task 1 (the hook registry) and proposals task 1 (I20, the production alias) | Planned |
| knowledge | `plan/stage-3-knowledge` | §8: notes and routes, the `VectorStore` (FTS5 + sqlite-vec), `SaveNote`, `SearchNotes`, the `knowledge` hook, re-embedding | I14, I17, I18 | `Knowledge` (I17) | after contracts task 1, embedders task 1 and data-summary task 1 | Planned |
| proposals | `plan/stage-3-proposals` | §9's mechanism: `Propose`, proposal records, superseding, the credential slots and their lifecycle, the `Handoffs` store and route, both cards | I13, I19, I20 | `Proposals` (I19) and `Handoffs` (I20) from task 1 (early PR) | after contracts tasks 2 and 3; task 4 also after connections task 1 (`inspect`, `ConnectionFindings`) | Planned |
| onboarding | `plan/stage-3-onboarding` | §9 applying both kinds, §10, §11: the wizard step, `connections` and `handoff` apply, the kickoff, the seven definitions (and removing `status_board:`), the end-to-end test | every plan above | the working flow | after every plan above merges | Planned |
| pages | `plan/stage-3-pages` | §12: Models, Data and Runs pages, each with its nav entry; the tracking sources section in project settings (list, add, credential, last sync) | the tracking, connections, table-detail and handoff routes; the tracking credential route | three pages and the sources section | after contracts task 2 and those routes are on `main` | Planned |

**contracts' tasks:** task 1 (early PR, small): "Shared by contracts" below except the payloads, and the models and protocols of I13 to I18 (I21's types are rate-limits', in `providers/failover.py`); five plans start on it. Task 2 (early PR): the proposal and handoff payloads (I19, I20) with their validators and `tzdata`; proposals, onboarding and pages need it. Task 3 (early PR): I13 in the approvals service, store and parts, the `propose` class in the policy, and the card dispatcher with `ApprovalControls`. Task 4: the regenerated `openapi.json` and the type checks for everything above.

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs. With ten plans and six people, an owner whose next task waits on another plan may claim a second plan (`AGENTS.md`).

## Interfaces between plans

contracts writes these into code exactly as here (except where a line names another producer); changing one takes a Decision issue. Fields are pydantic models unless marked Protocol. Changelog: 2026-10-28, the plan drafts' interface gaps were settled into these sections; later the same day, the fields the approved stage 4 and 5 specs need were added.

### Shared by contracts (task 1)

- ID prefixes `con`, `trk`, `prp`, `kno` in `ids.py`.
- Event types, in the backend `EventType` and the frontend `PROJECT_EVENT_TYPES`: `connection.created`, `connection.updated`, `connection.deleted`, `tracking.synced`, `proposal.created`, `proposal.decided`, `handoff.approved`, `turn.stalled`, `turn.resumed`, `note.saved`, `note.deleted`.
- `DUCKDB_VERSION = "2.0.0"` in `config.py`; dependencies `duckdb==2.0.0` and `sqlite-vec`.
- `credentials/project_secrets.py`: the class `ProjectSecrets(keystore, databases)` with `put(ref, value)`, `move(src_ref, dst_ref)`, `delete(ref)`, `delete_prefix(prefix)` and `is_set(ref)` (answered from `secret_refs`, never by reading the keystore), one instance built in `build_services` and passed to every consumer's constructor; and the module functions that are the only builders of ref strings: `proposal_slot_ref(project_id, proposal_id, slot)`, `proposal_prefix(project_id, proposal_id)`, `tracking_ref(project_id, source_id)`, `project_prefix(project_id)`; no other code writes a ref string; it records each ref it puts in the `secret_refs` table (ref, project, created), so `delete_prefix` works without listing the keystore. Refs: `project/<id>/proposal/<prp>/<slot>` and `project/<id>/tracking/<trk>`. Its consumers are tracking, proposals, onboarding's kinds and the project removal; no other code writes project secrets.
- The schema, in `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql` and `tables.py`: every table and column named in I13 to I21, plus `notes` (an explicit integer `seq` primary key and the text `id`), its FTS5 external-content table and triggers, `note_embeddings` (note ID primary key, embedder, dimensions, embedded_at; the vec0 tables use `notes.seq` as their rowid), `handoff_versions`, `kickoff` (handoff version, agent, posted message ID, posted_at, status), unique keys (source and key) on every tracking record table (`run_metrics`: source, run key, metric, step, time), `last_sync` and `last_error` on tracking sources, `secret_refs`, `turns.error_kind`, `providers.cooldown_minutes`, `handoff_versions` with UNIQUE `proposal_id`, and `connections` (`id, name UNIQUE, kind, settings_json, credential_ref, status, status_detail, created, updated`; `project.db` is per project, so no `project_id`). `notes_fts` is an FTS5 external-content table on `notes(title, text)` with `content_rowid='seq'` and insert, update and delete triggers; `note_embeddings` is keyed by `note_id`.
- `Provider.cooldown_minutes: int` (default 15) in `providers/models.py`.
- Removing a project deletes its secrets (`ProjectSecrets.delete_prefix(project_prefix(id))`, contracts' lines in `workspace/service.py`'s removal path).
- In contracts task 2, not task 1: the full `HandoffPayload` and `ConnectionsPayload` (I19) with their validators, and the I20 types.

### I13: approvals, parts and turns (changes to I9 and the approvals contract; types in contracts task 1, behavior in contracts task 3)

- `ApprovalKind = Literal["tool", "stall"]`. The approval record, `ApprovalView` and `ApprovalPart` gain `kind`, `step: int | None` and `proposal_kind: str | None`. For a `stall`, `tool`, `tool_call_id` and `risk_class` are null; existing rows are `tool`.
- `ToolContext` gains `tool_call_id` (I19 needs it).
- `Approvals` gains:
  - `create_stall(project_id, turn_id, step, preview) -> ApprovalView`: idempotent; a stall is unique among the pending approvals of its turn and step, so a later stall at the same step after Try again is allowed.
  - `cancel(project_id, approval_id) -> None`: marks a pending approval `cancelled` (used to supersede a proposal).
  - `add_observer(observer: ApprovalObserver)`, where `ApprovalObserver` (Protocol) has `async asked(view)` and `async decided(view)`; `decided` fires on approve, reject, cancel and expiry, and the service awaits every observer before it signals the turn to resume.
  - **One store.** `build_services` builds one `ApprovalStore` (holding the observers) and passes that instance to the gate, `ApprovalsService`, `SessionManager` (and through it `SessionStore`, `stop_waiting_turn`, `ToolStep`) and recovery expiry; no other code constructs one (contracts task 3, with a test through the real `build_services` wiring: `asked` fires for a gate-created approval, `decided` on Stop and on recovery expiry).
  - `cancel` resumes the turn when no approval of it is still pending.
  - `ApprovalView.tool` and `risk_class` become nullable, and `step`, `proposal_kind` and `kind` appear, in contracts task 3 (with the frontend lines that read them), never in task 1, so task 1's early PR keeps the frontend building.
- The gate sets `proposal_kind` on the approval from the `Propose` call's `kind` argument (`approvals/gate.py`).
- Deciding a stall: approve resumes the turn with no audit record; reject ends it `stopped`.
- A stall's preview is `Preview(kind="text", title="Stalled: no usable key", body=<per-key states>)`. A stall writes no audit record; events `turn.stalled {turn_id, provider, model}` and `turn.resumed {turn_id}` record it.
- contracts (task 1) adds the types and fields: `ErrorKind`, `TurnEndedData.error_kind`, `MessageView.error_kind`; rate-limits writes and reads them.
- `turn.ended` and the turn view gain `error_kind: Literal["keys_exhausted"] | None` (column `turns.error_kind`; rate-limits writes it through the executor's failure path, the `TurnObserver` protocol and its implementers); the failed agent message's `MessageView` carries the same `error_kind`, so the UI knows it after a reload; the UI offers Try again on a failed turn with that kind, which re-sends its trigger message.
- The policy gains the class `propose`: `ask` at every level, `bypass` included.
- Frontend: `ApprovalCard.tsx` dispatches on `kind` and `proposal_kind` to one component per card; contracts exports the note field with the Approve and Reject controls as one component (`ApprovalControls`) that every card reuses.

### I14: model types and embedders (`providers/`)

```python
ModelType = Literal["chat", "embedder"]
# ModelEntry gains: model_type: ModelType; dimensions: int | None   (embedders, set by verify)
# ModelCreate gains: model_type: ModelType = "chat"

class EmbedderUnavailable(Exception): ...      # every key exhausted or rate-limited (no wait), or unreachable; user-facing message

class Embedder(Protocol):
    address: str
    dimensions: int
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...   # raises EmbedderUnavailable

class EmbedderStatus(BaseModel): configured: bool; address: str | None; dimensions: int | None; problem: str | None

class Embedders(Protocol):            # injected into the services that need it, never looked up (embedders)
    async def for_project(self, project_id: str) -> Embedder | None: ...  # the project override, else the app default, else None
    async def status(self, project_id: str) -> EmbedderStatus: ...
```

### I15: connections (`connections/contract.py`)

```python
ConnectionStatus = Literal["ok", "unreachable"]
class TableRef(BaseModel): name: str; files: list[str]; format: Literal["csv", "parquet", "json", "jsonl"]
class ColumnInfo(BaseModel): name: str; type: str
class TableDetail(BaseModel): table: TableRef; columns: list[ColumnInfo]; row_count: int; data_version: str; summary: str   # served by data-summary from its cache
class Mount(BaseModel): host_path: Path; container_path: str        # always read-only
class DuckDbSource(BaseModel): sql_from: str; allowed_directories: list[Path]
class Finding(BaseModel): path: str; reason: str
class Findings(BaseModel): unsupported_files: list[Finding]; secret_files: list[Finding]; truncated: bool
class ConnectionView(BaseModel): id: str; name: str; kind: str; settings: dict[str, Any]; status: ConnectionStatus; status_detail: str | None; findings: Findings; data_version: str | None   # the data version of the table whose newest file has the latest modified time

class ConnectionKind(Protocol):
    name: str
    settings_model: type[BaseModel]
    needs_credential: bool
    def validate(self, settings: BaseModel, credential: str | None) -> None: ...   # raises with a user-facing reason
    def findings(self, settings: BaseModel) -> Findings: ...
    def tables(self, settings: BaseModel) -> list[TableRef]: ...
    def data_version(self, settings: BaseModel, table: TableRef) -> str: ...
    def duckdb_source(self, settings: BaseModel, table: TableRef) -> DuckDbSource: ...
    def sandbox_mount(self, name: str, settings: BaseModel) -> Mount | None: ...   # container path /data/<name>

class Connections(Protocol):        # connections
    async def list(self, project_id: str) -> list[ConnectionView]: ...
    async def inspect(self, project_id: str, kind: str, settings: dict[str, Any]) -> Findings: ...   # validates and reports, creates nothing
    async def create(self, project_id: str, kind: str, name: str, settings: dict[str, Any]) -> ConnectionView: ...
    async def update(self, project_id: str, connection_id: str, settings: dict[str, Any]) -> ConnectionView: ...
    async def delete(self, project_id: str, connection_id: str) -> None: ...
    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]: ...
    async def data_version(self, project_id: str, connection_id: str, table: str) -> str: ...
    async def duckdb_source(self, project_id: str, connection_id: str, table: str) -> DuckDbSource: ...
    async def mounts(self, project_id: str) -> list[Mount]: ...
```

- The findings' text is the backend's (`Finding.reason`); connections owns the one frontend component that shows a connection's findings, secret and unsupported files alike (`ConnectionFindings`), which its settings dialog, the proposals' connections card and the Data page all import; none writes findings text of its own.
- Relative paths are refused, and so are a folder inside the app's data folder and a folder that contains it. Local folders work in both run modes: in server mode they are folders on the server, picked with the server-side folder browser; connections task 1 lifts the server-mode refusal from `GET /api/folders` (`WorkspaceService.browse`, its one change in `workspace/`), which stays behind the session like every route.
- Routes (connections): `GET/POST /api/projects/{id}/connections`, `POST .../connections/inspect`, `PATCH/DELETE .../connections/{con}`, `GET .../connections/{con}/tables`. Route (data-summary): `GET .../connections/{con}/tables/{table}` returning `TableDetail`.

### I16: tracking (`tracking/contract.py`)

```python
SourceType = Literal["mlflow", "github_actions"]
RunStatus = Literal["running", "succeeded", "failed", "cancelled", "other"]   # adapters normalise to these
class TrackingSourceView(BaseModel): id: str; type: SourceType; settings: dict[str, Any]; auth: Literal["none", "credential"]; credential_set: bool; last_sync: datetime | None; last_error: str | None
class ModelVersionRow(BaseModel): source_id: str; model: str; version: str; run_key: str | None; aliases: list[str]; created: datetime; evaluation: Literal["present", "absent", "fetch_failed"]   # absent: the run has no evaluation.json; fetch_failed: it could not be read (stage 4 then reads the results-location copy)
class ModelSummary(BaseModel): source_id: str; name: str; production_version: str | None; latest_version: str | None; versions: int
class RunRow(BaseModel): source_id: str; key: str; experiment: str; status: RunStatus; start: datetime | None; end: datetime | None; params: dict[str, str]; tags: dict[str, str]; git_commit: str | None; datasets: list[str]
class MetricPoint(BaseModel): source_id: str; run_key: str; metric: str; step: int; value: float; time: datetime
class DeploymentRow(BaseModel): source_id: str; model: str; alias: str; from_version: str | None; to_version: str | None; seen_at: datetime   # to_version None: the alias was removed
class CiRunRow(BaseModel): source_id: str; key: str; workflow: str; status: RunStatus; start: datetime; duration_s: float | None; commit: str; url: str
class EvaluationRecord(BaseModel): source_id: str; model: str; version: str; run_key: str; schema_version: int; payload: dict[str, Any]; fetched_at: datetime   # mlops_agents/evaluation.json (stage 5)
class SyncResult(BaseModel): versions: list[ModelVersionRow]; runs: list[RunRow]; metrics: list[MetricPoint]; aliases: dict[str, dict[str, str]]; ci_runs: list[CiRunRow]; evaluations: list[EvaluationRecord]; error: str | None   # aliases: model -> alias -> version; Tracking derives DeploymentRows
class Page[T](BaseModel): rows: list[T]; total: int; truncated: bool
class TrackingQuery(BaseModel): what: Literal["models", "versions", "runs", "metrics", "deployments", "ci_runs", "evaluations"]; model: str | None = None; version: str | None = None; source_id: str | None = None; run_key: str | None = None; metric: str | None = None; latest: bool = False; since: datetime | None = None; limit: int = 50; offset: int = 0   # limit max 500

class Tracking(Protocol):           # tracking
    async def sources(self, project_id: str) -> list[TrackingSourceView]: ...
    async def create_source(self, project_id: str, type: SourceType, settings: dict[str, Any], auth: Literal["none", "credential"]) -> TrackingSourceView: ...
    async def sync(self, project_id: str, source_id: str | None = None) -> None: ...   # starts a sync; tracking.synced reports its end
    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]: ...   # rows typed by `what`; TrackingHistory and the routes read it
class TrackingAdapter(Protocol):
    type: SourceType
    async def sync(self, source: TrackingSourceView, credential: str | None, since: datetime | None) -> SyncResult: ...
```

- Source settings, one model per type, `extra="forbid"`: `MlflowSettings(url: str)`, `GithubActionsSettings(repository: str, workflows: list[str] = [])`; onboarding's `TrackingSourceSpec.settings` uses these keys.
- `ModelSummary.production_version` is the version holding the alias that the current handoff's `ModelSpec.environments` maps to the environment `production` (read through `Handoffs.current`, I20); without a handoff, the alias `production`.
- A `github_actions` source has `auth: "none"` and uses the app's GitHub login.
- The MLflow adapter fetches `mlops_agents/evaluation.json` for model-version runs through MLflow's `GET /get-artifact` (a server that cannot serve the artifact yields no record, not an error). Records are stored in the `evaluations` table (source, model, version, run_key, schema_version, payload JSON, fetched_at), unique on source, model and version (contracts).
- Routes `GET` and `PUT .../tracking/settings` (`{sync_interval_s}`) set the sync interval (tracking).
- The default sync interval is 1 minute (a project setting).
- Routes: `GET/POST /api/projects/{id}/tracking/sources`; `PUT .../tracking/sources/{trk}/credential` (keystore only); `POST .../tracking/sync` with an optional `source_id`, returning 202; `GET .../tracking/models` (`Page[ModelSummary]`), `.../versions?model=` (`Page[ModelVersionRow]`), `.../runs` (`Page[RunRow]`), `.../ci-runs` (`Page[CiRunRow]`), `.../deployments` (`Page[DeploymentRow]`), `.../metrics?run_key=&metric=&latest=` (`Page[MetricPoint]`), `.../evaluations?model=&version=` (`Page[EvaluationRecord]`). List routes take `limit` (default 50, max 500) and `offset`; `/runs` takes `run_key`; `/versions` and `/deployments` take `model` and `source_id`.
- The fake MLflow server (tracking) runs in-process as an ASGI app and standalone (`python -m tests.fakes.fake_mlflow --port <n>`) for the e2e.

### I17: knowledge (`knowledge/contract.py`)

```python
NoteType = Literal["fact", "decision", "incident", "runbook", "doc"]
class NoteDraft(BaseModel): type: NoteType; title: str; text: str; source: str | None = None
class NoteView(BaseModel): id: str; type: NoteType; title: str; text: str; source: str | None; author: str; created: datetime; updated: datetime
class SearchResult(BaseModel): notes: list[NoteView]; used_vectors: bool; detail: str | None   # why vectors were not used
class VectorStore(Protocol):
    async def upsert(self, project_id: str, note_id: str, vector: list[float], embedder: str) -> None: ...
    async def remove(self, project_id: str, note_id: str) -> None: ...
    async def nearest(self, project_id: str, vector: list[float], embedder: str, k: int) -> list[str]: ...
    async def drop_embedder(self, project_id: str, embedder: str) -> None: ...
class Knowledge(Protocol):          # knowledge
    async def save(self, project_id: str, author: str, note: NoteDraft, note_id: str | None = None) -> NoteView: ...
    async def get(self, project_id: str, note_id: str) -> NoteView | None: ...
    async def search(self, project_id: str, query: str, type: NoteType | None, limit: int) -> SearchResult: ...
    async def delete(self, project_id: str, note_id: str) -> None: ...
```

- An embedder change is noticed lazily: vectors carry their embedder, and a save or search that finds a different one starts the background re-embed.
- Routes: `GET/POST /api/projects/{id}/notes`, `PATCH/DELETE .../notes/{kno}`.

### I18: context hooks

```python
# agents/hooks/contract.py (contracts)
class HookContext(BaseModel): project_id: str; agent: str; trigger_text: str
class ContextHook(Protocol):
    name: str
    async def render(self, ctx: HookContext) -> str: ...   # capped text; never raises (a failure renders as one line)

# agents/hooks/registry.py (data-summary)
class HookRegistry:
    def register(self, hook: ContextHook) -> None: ...
    def names(self) -> frozenset[str]: ...
    async def run(self, names: Sequence[str], ctx: HookContext) -> str: ...
```

- Hook output is untrusted data: the injection point wraps it in a marked block (`<data source="hook:name">…</data>`), and the shared working rules (onboarding) tell agents it is data, never instructions.
- Hooks run when a turn starts and again when a waiting turn resumes (so Onboarding sees the data summary after the connections are approved, and the Orchestrator keeps its status board).
- The registry replaces `KNOWN_HOOKS` in `agents/catalog.py`; the catalog validates `context_hooks` against `registry.names()`. The one injection point (today the status summary in `agents/runtime/agent.py`) calls `HookRegistry.run`. `status_board` is the first registered hook; until onboarding rewrites the definitions, the catalog treats `status_board: true` as that hook, and onboarding then removes the field from every definition and from `AgentDefinition`.
- Each hook-producing plan registers its hook in `build_services` after data-summary's registry exists and before `AgentCatalog.load`.

### I19: proposals (`proposals/contract.py`)

```python
ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]
class CredentialSlot(BaseModel): index: int; label: str; source_type: str
class SlotState(BaseModel): index: int; label: str; set: bool
class ApplyResult(BaseModel): applied: list[str]; failed: dict[str, str]   # part -> reason
class ProposalView(BaseModel): id: str; kind: str; key: str; payload: ConnectionsPayload | HandoffPayload; warnings: list[str]; status: ProposalStatus; agent: str; approval_id: str | None; turn_id: str; tool_call_id: str; result: ApplyResult | None; slots: list[SlotState]; created: datetime   # contracts task 2 (it needs the payloads); typed so the cards read it from schema.d.ts

# Payloads (contracts), extra="forbid" everywhere
LocalFolderKindName = Literal["local_folder"]; LOCAL_FOLDER_KIND: LocalFolderKindName = "local_folder"   # connections/contract.py (contracts task 1): one source; a test pins the two together
class LocalFolderSettings(BaseModel): path: str   # connections/contract.py (contracts task 1), extra="forbid"
class ConnectionSpec(BaseModel): kind: LocalFolderKindName; name: str; settings: LocalFolderSettings; why: str      # name follows CONNECTION_NAME_PATTERN (connections/contract.py, contracts task 1); settings typed per kind, so no secret can ride in a free-form dict
class ConnectionsPayload(BaseModel): connections: list[ConnectionSpec]

class TableUse(BaseModel): connection: str; table: str                                        # a reference to a DataRole
class DataRole(BaseModel): connection: str; table: str; role: Literal["training", "serving_inputs", "predictions", "labels", "other"]; id_column: str | None; time_column: str | None   # serving_inputs = the model's raw inputs as served
class GroupSpec(BaseModel): column: str; values: list[str] | None
class Profile(BaseModel): objective: str; problem_type: str; target_column: str; positive_class: str | None; primary_metric: str; metric_rationale: str; operating_threshold: float | None; label_delay_days: int; label_maturity_days: int; serving_mode: Literal["batch", "online", "streaming"]; error_costs: str; cost_ratio: float | None; groups: list[GroupSpec]
Placeholder = Literal["request_id", "model", "candidate_version", "champion_version", "window_start", "window_end", "mode", "threshold"]
class CommandSpec(BaseModel): kind: Literal["train", "evaluate", "deploy", "rollback", "rerun", "hold", "fall_back"]; type: Literal["command", "ci_dispatch"]; template: str | None; workflow: str | None; inputs: dict[str, str]; placeholders: list[Placeholder]; deploys: bool; entry_files: list[str]
class ProductionSignal(BaseModel): kind: Literal["tracker_alias", "ci_job", "file"]; ref: str
class ModelSpec(BaseModel): name: str; profile: Profile; predictions: TableUse | None; labels: TableUse | None; commands: list[CommandSpec]; promotion: Literal["team_gate", "person"]; environments: dict[str, str]; production_signal: ProductionSignal | None   # environments: tracker alias -> environment
class PipelineStage(BaseModel): name: str; entry: str; inputs: list[str]; outputs: list[str]
class PipelineEdge(BaseModel): from_stage: str; to_stage: str
class Cadence(BaseModel): cron: str | None; every_minutes: int | None      # exactly one is set; cron is standard five-field
class DataFeedRef(BaseModel): connection: str; table: str
class JobFeedRef(BaseModel): ci_workflow: str | None; file: FileResults | None   # exactly one set
class Feed(BaseModel): name: str; kind: Literal["data", "job"]; ref: DataFeedRef | JobFeedRef; cadence: Cadence; grace_minutes: int; timezone: str
class OnCallHours(BaseModel): days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]; start: str; end: str   # "HH:MM"
class Contact(BaseModel): channel: Literal["discord", "email"]; address: str
class OnCall(BaseModel): name: str; contacts: list[Contact]; hours: OnCallHours; timezone: str; escalation_order: int   # contacts: one or more channels (critical alerts go to all)
class FileResults(BaseModel): connection: str; path_pattern: str
class CiResults(BaseModel): workflow: str
class MlflowResults(BaseModel): metric: str; threshold: float | None; fires_when: Literal["above", "below"] | None
class CheckSpec(BaseModel): check_id: str; name: str; category: Literal["data_quality", "drift", "model_performance", "operational"]; cadence: Cadence; timezone: str; results: FileResults | CiResults | MlflowResults
class Scheduler(BaseModel): name: str; config_location: str
class Operations(BaseModel): scheduler: Scheduler | None; feeds: list[Feed]; on_call: list[OnCall]; checks: list[CheckSpec]; results_location: FileResults | MlflowResults
class TrackingSourceSpec(BaseModel): type: SourceType; settings: MlflowSettings | GithubActionsSettings; auth: Literal["none", "credential"]   # settings models in tracking/contract.py (contracts task 1), extra="forbid"; MlflowSettings.url passes base_url_problem (no userinfo)
class Gap(BaseModel): what: str; next_step: str
class Grants(BaseModel): network: bool; permission_level: GrantableLevel    # GrantableLevel = PermissionLevel minus "bypass"
class HandoffPayload(BaseModel): models: list[ModelSpec]; commands: list[CommandSpec]; data_roles: list[DataRole]; tracking: list[TrackingSourceSpec]; pipeline: list[PipelineStage]; edges: list[PipelineEdge]; operations: Operations; gaps: list[Gap]; grants: Grants   # commands: project-level (rerun a data job, hold or quarantine a batch)

CONNECTIONS_KIND = "connections"; HANDOFF_KIND = "handoff"   # the one source of the kind names (contracts, proposals/contract.py)
class ProposalKind(Protocol):       # each kind: preview, warnings, credentials_needed and apply by onboarding; its card by proposals
    name: str
    payload_model: type[BaseModel]
    def key(self, payload: BaseModel) -> str: ...   # the superseding key within the project (proposals live in project.db): a constant for connections and handoff; later kinds choose (a promotion: its model)
    def preview(self, payload: BaseModel) -> Preview: ...
    def warnings(self, payload: BaseModel) -> list[str]: ...   # user-facing text (for example the network sentence for an MLflow source with auth "none"); the card shows them as given
    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]: ...
    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult: ...   # idempotent

class ProposalKinds:                # proposals; onboarding registers both kinds in build_services
    def register(self, kind: ProposalKind) -> None: ...
    def get(self, name: str) -> ProposalKind: ...

class Proposals(Protocol):          # proposals
    async def create(self, project_id: str, agent: str, kind: str, key: str, payload: dict[str, Any], turn_id: str, tool_call_id: str) -> ProposalView: ...   # a draft: idempotent by (turn_id, tool_call_id); approval_id is None until attached
    async def attach(self, project_id: str, proposal_id: str, approval_id: str) -> str | None: ...   # called on asked(view); returns the approval ID of a pending proposal it supersedes, which the observer then cancels (proposals never holds Approvals, so build_services has no cycle)
    async def get_by_approval(self, project_id: str, approval_id: str) -> ProposalView | None: ...
    async def pending(self, project_id: str, kind: str, key: str) -> ProposalView | None: ...   # superseding is by kind and key; key = a constant for connections and handoff (proposals are per project)
    async def latest_for_turn(self, project_id: str, turn_id: str, kind: str) -> ProposalView | None: ...
```

- **A duplicate never gets an approval.** A second `Propose` of the same kind in the same turn step fails in `preview` with a tool error, so the gate creates no approval for it.
- **The record before the approval.** `Propose.preview` (run by the gate before it creates the approval) calls `create`, which writes a draft keyed by `(turn_id, tool_call_id)`: a re-run of the same call finds its draft. `asked(view)` then calls `attach` with the approval ID (the approval carries the tool call ID).
- `ToolContext` gains `tool_call_id: str` (contracts, in `agents/tools/base.py`).
- proposals registers an `ApprovalObserver`: on `decided` it marks the proposal, applies it on approval, and deletes its credential slots on reject, cancel or expiry. A newer proposal of the same kind cancels the pending one (`Approvals.cancel`). At startup, an approved proposal with no `result` is applied again, and orphaned slots are deleted.
- Routes: `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}` (keystore only; refused unless pending); `GET .../proposals/by-approval/{apr}`. Events `proposal.created`, `proposal.decided`, `handoff.approved`.

### I20: the current handoff (types in `proposals/contract.py` by contracts; `SqliteHandoffs` in `proposals/handoffs.py` by proposals)

```python
class HandoffView(BaseModel): proposal_id: str; version: int; approved: datetime; payload: HandoffPayload
class Handoffs(Protocol):
    async def current(self, project_id: str) -> HandoffView | None: ...
    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int: ...   # idempotent: one version per proposal (UNIQUE proposal_id in handoff_versions); returns that version
```

- Route: `GET /api/projects/{id}/handoff` returns `HandoffView` or `null` (200). Stage 4 reads `operations` from it: the baseline inventory.
- Agents read the current handoff with the tool `ReadHandoff(section?)` (risk class `read`; justified: it is in the app's database), owned by onboarding; a section returns that part of the payload, capped.
- The payload models validate in code (contracts): IANA time zones, on-call hours (an `end` before `start` means the window crosses midnight), a `command` has a `template` and a `ci_dispatch` a `workflow`, every `{placeholder}` in a template is declared, `Cadence` has exactly one field, references between parts (`TableUse`, alias maps, edges) resolve.

### I21: key failover (`providers/failover.py`; rate-limits task 1)

```python
KeyStatus = Literal["ok", "limited", "exhausted_daily", "billing", "rejected"]
class KeyState(BaseModel): key_index: int; status: KeyStatus; since: datetime | None

class AllKeysExhausted(Exception): keys: list[KeyState]
class RateLimited(Exception): retry_after_s: float           # raised instead of waiting when wait=False
class StreamedTextFailure(Exception): cause: BaseException    # raised by an attempt after text has streamed; never failed over

class KeyFailover:   # one per app (build_services), shared by every call; state per (provider id, key index, model)
    async def call[T](self, provider: Provider, model: str, attempt: Callable[[int | None], Awaitable[T]], *, cooldown_minutes: int, wait: bool = True) -> T: ...   # callers pass Provider.cooldown_minutes
```

- `attempt(key_index)` raises the provider's raw exception; `KeyFailover` classifies it with `providers/errors.classify`. A provider with no keys gets `attempt(None)` once, with no failover.
- `KeyFailover` also exposes `states(provider, model) -> list[KeyState]`, `clear(provider) -> None` (Try again) and `describe_keys(states) -> str` (the one wording, used by the stall card and the embedder's message). Its clock is injected (`Clock` protocol: `now()`, `async sleep(seconds)`).
- `TurnHooks` gains `approvals: Approvals`, `approval_store: ApprovalStore` (the one shared store, which `graph.py` uses instead of constructing its own) and `events: EventLog`, so the model node can create a stall and record `turn.stalled` and `turn.resumed`.
- The one `KeyFailover` instance is built in `build_services` by rate-limits task 1 (its early PR), so embedders task 1 can use it.
- `KeyFailover` takes the cooldown per call (`cooldown_minutes` argument), so task 1 does not depend on contracts; task 2 passes `Provider.cooldown_minutes`.
- The rules are the spec's §2. A wait round lasts at most until the earliest key clears, never more than 60 seconds.

## Files shared by these plans

Each file below is edited by one plan only, except where a line says otherwise:
- `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`, `ids.py`, `config.py`, `credentials/project_secrets.py`, `agents/approvals/service.py`, `agents/approvals/gate.py`, `agents/approvals/store*`, the event type lists: contracts. contracts also adds `cooldown_minutes` to `providers/models.py` in task 1 (its only line there). A later plan that needs a change opens a Decision.
- `agents/runtime/model_caller.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/runtime/waiting.py` (rate-limits builds a stall's part there; contracts adds its one line setting `proposal_kind` on a `Propose` call's part), `providers/errors.py`, `providers/failover.py`: rate-limits; it also adds its key-failover lines to `providers/service.py`, its stall lines to `agents/runtime/sessions.py`, the `error_kind` path (`agents/runtime/executor.py`, the `TurnObserver` protocol and implementers, `MessageView`), and the cooldown field's lines to `api/routers/providers.py`.
- `providers/models.py`, `providers/service.py`, `providers/verification.py`: embedders, except the lines named for contracts and rate-limits.
- `agents/runtime/agent.py` (the injection point), `agents/catalog.py`: data-summary, which also adds its five lines to `agents/runtime/executor.py` and its builder construction to `agents/runtime/sessions.py` (onboarding later removes the `status_board` field from `catalog.py`).
- `sandbox/containers.py`, `sandbox/templates/*`: connections.
- `agents/tools/__init__.py` (`default_registry`) and `api/services.py`: each plan adds only its own lines.
- `agents/runtime/context.py`: data-summary changes `build_messages` (the injection point); onboarding adds one line (the shared working rules: hard limits and the continuous-PR principle, from packaged files).
- `agents/runtime/hooks.py`: contracts (`GateDecision`) and rate-limits (`TurnObserver`), each its own lines.
- The five `ApprovalStore(` construction sites (`approvals/gate.py`, `approvals/service.py`, `agents/runtime/graph.py`, `agents/runtime/store/__init__.py`, `agents/runtime/waiting.py`): contracts task 3 replaces each with the injected instance (graph.py receives it through `TurnHooks.approval_store`), its only lines in the rate-limits-owned files; tests that construct `ApprovalGate`, `ApprovalsService` or `SessionManager` pass an explicit store (a shared test helper builds one), and task 3 lists those test files by grep.
- `frontend/src/pages/project-settings/ProjectSettingsPage.tsx`: embedders, connections and pages each add one line (their section).
- `workspace/service.py`: contracts (the project removal's secret cleanup) and connections (lifting the server-mode refusal in `browse`), each its own lines.
- New package `__init__.py` files: `tracking/`, `connections/`, `knowledge/`, `agents/hooks/` by contracts task 1, `proposals/` by contracts task 2, `onboarding/` by onboarding; other plans add modules, never recreate them. Sub-packages (`connections/kinds/`, `tracking/adapters/`, `data/`) belong to the plan that creates them.
- `frontend/src/pages/inbox/InboxPage.test.tsx`: contracts and rate-limits, each its own tests.
- `backend/tests/agents/test_catalog.py`: data-summary and onboarding, each its own tests.
- `frontend/src/pages/agents/MessageView.tsx`: contracts (the part dispatch) and rate-limits (Try again on `error_kind`), each its own lines.
- `agents/definitions/*.md`: onboarding, for stage 3; the stage 4 and 5 plans edit them later, named in their overviews.
- `tests/fakes/fake_openai.py`: embedders (`/embeddings`), rate-limits (429 and 402 scripts) and onboarding (its scripted models), each in its own functions.
- `frontend/src/pages/agents/ApprovalCard.tsx`: contracts (the dispatcher and `ApprovalControls`); rate-limits (the stall card) and proposals (the two proposal cards) each add their own component file and one line in the dispatcher.
- `frontend/e2e/flow.spec.ts`: onboarding only.
- Pages: each page's `route.tsx` carries its own nav entry, so the route table and `Sidebar.tsx` need no edit.

