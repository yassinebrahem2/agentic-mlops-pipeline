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
- Frontend types come only from the generated `schema.d.ts` (`npm run gen:api`).

## Plans

| Plan | Branch | Covers | Consumes (besides merged code) | Produces | Starts |
|---|---|---|---|---|---|
| contracts (claim first) | `plan/stage-3-contracts` | I13 to I20 as models and protocols; the stage 3 schema; the `propose` class; the `stall` kind in the approvals store; the approval-card dispatcher | none | I13 to I20 in code, both migrations, the dispatcher | now |
| rate-limits | `plan/stage-3-rate-limits` | §2: the key failover engine (per key and model), the error classification, the stall (model node, graph routing, card, Try again), the cooldown setting | task 1 none; the rest I13 (contracts task 2) | `KeyFailover` (I21) from task 1 (early PR); the stall | task 1 now; the rest after contracts task 2 |
| embedders | `plan/stage-3-embedders` | §3: model types, the embedder verify step, type-filtered pickers, the embedder settings, the fake `/embeddings`, the `Embedders` service | I14; I21 | `Embedders` (I14) from task 1 (early PR) | after contracts task 1 and rate-limits task 1 |
| connections | `plan/stage-3-connections` | §4, §5: the registry and its routes, the `local_folder` kind, the settings section, read-only mounts and the container fingerprint, DuckDB in the environment image, the host-loopback test | I15 | `Connections` (I15) and its routes from task 1 (early PR) | after contracts task 1 |
| data-summary | `plan/stage-3-data-summary` | §6: the hook registry and the one injection point, `status_board` as a hook, `data_summary` with the DuckDB lock-down, the cache, the table-detail route | I15, I18 | `HookRegistry` (I18) from task 1 (early PR); `GET .../connections/{con}/tables/{table}` | after contracts task 1 and connections task 1 |
| tracking | `plan/stage-3-tracking` | §7: sources and routes, the MLflow and GitHub Actions adapters, the fake MLflow server, the sync loop, `TrackingHistory`, `tracking_summary` | I16, I18 | `Tracking` (I16) and its routes from task 1 (early PR) | after contracts task 1 and data-summary task 1 |
| knowledge | `plan/stage-3-knowledge` | §8: notes and routes, the `VectorStore` (FTS5 + sqlite-vec), `SaveNote`, `SearchNotes`, the `knowledge` hook, re-embedding | I14, I17, I18 | `Knowledge` (I17) | after contracts task 1, embedders task 1 and data-summary task 1 |
| proposals | `plan/stage-3-proposals` | §9's mechanism: `Propose`, proposal records, superseding, the credential slots and their lifecycle, the `Handoffs` store and route, both cards | I13, I19, I20 | `Proposals` (I19) and `Handoffs` (I20) from task 1 (early PR) | after contracts task 2 |
| onboarding | `plan/stage-3-onboarding` | §9 applying both kinds, §10, §11: the wizard step, `connections` and `handoff` apply, the kickoff, the seven definitions (and removing `status_board:`), the end-to-end test | every plan above | the working flow | after every plan above merges |
| pages | `plan/stage-3-pages` | §12: Models, Data and Runs pages, each with its nav entry | the tracking, connections, table-detail and handoff routes | three pages | after those routes are on `main` |

**contracts' tasks:** task 1 (early PR): "Shared by contracts" below, and every model and protocol of I13 to I20 (types and the schema only, mergeable within a day). Task 2 (early PR): I13 in the approvals service, store and parts, the `propose` class in the policy, and the card dispatcher with `ApprovalControls`. Task 3: the regenerated `openapi.json` and frontend types for everything above.

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs.

## Interfaces between plans

contracts writes these into code exactly as here (except where a line names another producer); changing one takes a Decision issue. Fields are pydantic models unless marked Protocol. Changelog: 2026-10-28, the plan drafts' interface gaps were settled into these sections.

### Shared by contracts (task 1)

- ID prefixes `con`, `trk`, `prp`, `kno` in `ids.py`.
- Event types, in the backend `EventType` and the frontend `PROJECT_EVENT_TYPES`: `connection.created`, `connection.updated`, `connection.deleted`, `tracking.synced`, `proposal.created`, `proposal.decided`, `handoff.approved`, `turn.stalled`, `turn.resumed`, `note.saved`, `note.deleted`.
- `DUCKDB_VERSION = "2.0.0"` in `config.py`; dependencies `duckdb==2.0.0` and `sqlite-vec`.
- `credentials/project_secrets.py`: `put(ref, value)`, `move(src_ref, dst_ref)`, `delete(ref)`, `delete_prefix(prefix)` over `KeyStore`. Refs: `project/<id>/proposal/<prp>/<slot>` and `project/<id>/tracking/<trk>`. Used by the proposal slot route and the tracking credential route; nothing else writes project secrets.
- The schema, in `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql` and `tables.py`: every table and column named in I13 to I21, plus `notes` (an explicit integer `seq` primary key and the text `id`), its FTS5 external-content table and triggers, `note_embeddings` (note, embedder, dimensions), `handoff_versions`, `kickoff` (project, handoff version, agent, posted message ID, status), unique keys (source and key) on every tracking record table, `last_sync` and `last_error` on tracking sources.

### I13: approvals, parts and turns (changes to I9 and the approvals contract; contracts task 2)

- `ApprovalKind = Literal["tool", "stall"]`. The approval record, `ApprovalView` and `ApprovalPart` gain `kind`, `step: int | None` and `proposal_kind: str | None`. For a `stall`, `tool`, `tool_call_id` and `risk_class` are null; existing rows are `tool`.
- `Approvals` gains:
  - `create_stall(project_id, turn_id, step, preview) -> ApprovalView`: idempotent; a stall is unique among the pending approvals of its turn and step, so a later stall at the same step after Try again is allowed.
  - `cancel(project_id, approval_id) -> None`: marks a pending approval `cancelled` (used to supersede a proposal).
  - `add_observer(observer: ApprovalObserver)`, where `ApprovalObserver` (Protocol) has `async asked(view)` and `async decided(view)`; `decided` fires on approve, reject, cancel and expiry.
- A stall's preview is `Preview(kind="text", title="Stalled: no usable key", body=<per-key states>)`. A stall writes no audit record; events `turn.stalled {turn_id, provider, model}` and `turn.resumed {turn_id}` record it.
- `turn.ended` and the turn view gain `error_kind: Literal["keys_exhausted"] | None`; the UI offers Try again on a failed turn with that kind, which re-sends its trigger message.
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
class Findings(BaseModel): unsupported_files: list[str]; secret_files: list[str]
class ConnectionView(BaseModel): id: str; name: str; kind: str; settings: dict[str, Any]; status: ConnectionStatus; status_detail: str | None; findings: Findings

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

- Relative paths are refused, and so are a folder inside the app's data folder and a folder that contains it.
- Routes (connections): `GET/POST /api/projects/{id}/connections`, `POST .../connections/inspect`, `PATCH/DELETE .../connections/{con}`, `GET .../connections/{con}/tables`. Route (data-summary): `GET .../connections/{con}/tables/{table}` returning `TableDetail`.

### I16: tracking (`tracking/contract.py`)

```python
SourceType = Literal["mlflow", "github_actions"]
RunStatus = Literal["running", "succeeded", "failed", "cancelled", "other"]   # adapters normalise to these
class TrackingSourceView(BaseModel): id: str; type: SourceType; settings: dict[str, Any]; auth: Literal["none", "credential"]; credential_set: bool; last_sync: datetime | None; last_error: str | None
class ModelVersionRow(BaseModel): source_id: str; model: str; version: str; run_key: str | None; aliases: list[str]; created: datetime
class ModelSummary(BaseModel): source_id: str; name: str; production_version: str | None; latest_version: str | None; versions: int
class RunRow(BaseModel): source_id: str; key: str; experiment: str; status: RunStatus; start: datetime | None; end: datetime | None; params: dict[str, str]; tags: dict[str, str]; git_commit: str | None; datasets: list[str]
class MetricPoint(BaseModel): source_id: str; run_key: str; metric: str; step: int; value: float; time: datetime
class DeploymentRow(BaseModel): source_id: str; model: str; alias: str; from_version: str | None; to_version: str | None; seen_at: datetime   # to_version None: the alias was removed
class CiRunRow(BaseModel): source_id: str; key: str; workflow: str; status: RunStatus; start: datetime; duration_s: float | None; commit: str; url: str
class SyncResult(BaseModel): versions: list[ModelVersionRow]; runs: list[RunRow]; metrics: list[MetricPoint]; aliases: dict[str, dict[str, str]]; ci_runs: list[CiRunRow]; error: str | None   # aliases: model -> alias -> version; Tracking derives DeploymentRows
class Page[T](BaseModel): rows: list[T]; total: int; truncated: bool
class TrackingQuery(BaseModel): what: Literal["models", "versions", "runs", "metrics", "deployments", "ci_runs"]; model: str | None = None; run_key: str | None = None; metric: str | None = None; since: datetime | None = None; limit: int = 50

class Tracking(Protocol):           # tracking
    async def sources(self, project_id: str) -> list[TrackingSourceView]: ...
    async def create_source(self, project_id: str, type: SourceType, settings: dict[str, Any], auth: Literal["none", "credential"]) -> TrackingSourceView: ...
    async def sync(self, project_id: str, source_id: str | None = None) -> None: ...   # starts a sync; tracking.synced reports its end
    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]: ...   # rows typed by `what`; TrackingHistory and the routes read it
class TrackingAdapter(Protocol):
    type: SourceType
    async def sync(self, source: TrackingSourceView, credential: str | None, since: datetime | None) -> SyncResult: ...
```

- A `github_actions` source has `auth: "none"` and uses the app's GitHub login.
- Routes: `GET/POST /api/projects/{id}/tracking/sources`; `PUT .../tracking/sources/{trk}/credential` (keystore only); `POST .../tracking/sync` with an optional `source_id`, returning 202; `GET .../tracking/models` (`Page[ModelSummary]`), `.../versions?model=` (`Page[ModelVersionRow]`), `.../runs` (`Page[RunRow]`), `.../ci-runs` (`Page[CiRunRow]`), `.../deployments` (`Page[DeploymentRow]`), `.../metrics?run_key=&metric=` (`Page[MetricPoint]`).
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

- The registry replaces `KNOWN_HOOKS` in `agents/catalog.py`; the catalog validates `context_hooks` against `registry.names()`. The one injection point (today the status summary in `agents/runtime/agent.py`) calls `HookRegistry.run`. `status_board` is the first registered hook; until onboarding rewrites the definitions, the catalog treats `status_board: true` as that hook, and onboarding then removes the field from every definition and from `AgentDefinition`.
- Each hook-producing plan registers its hook in `build_services` after data-summary's registry exists and before `AgentCatalog.load`.

### I19: proposals (`proposals/contract.py`)

```python
ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]
class CredentialSlot(BaseModel): index: int; label: str; source_type: str
class SlotState(BaseModel): index: int; label: str; set: bool
class ApplyResult(BaseModel): applied: list[str]; failed: dict[str, str]   # part -> reason
class ProposalView(BaseModel): id: str; kind: str; payload: dict[str, Any]; status: ProposalStatus; agent: str; approval_id: str; turn_id: str; result: ApplyResult | None; slots: list[SlotState]; created: datetime
class ConnectionsPayload, HandoffPayload     # exactly the spec's §9 fields (contracts); HandoffPayload.operations.checks: check_id, name, category, cadence, results (file | ci | mlflow)

class ProposalKind(Protocol):       # each kind: preview, credentials_needed and apply by onboarding; its card by proposals
    name: str
    payload_model: type[BaseModel]
    def preview(self, payload: BaseModel) -> Preview: ...
    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]: ...
    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult: ...   # idempotent

class ProposalKinds:                # proposals; onboarding registers both kinds in build_services
    def register(self, kind: ProposalKind) -> None: ...
    def get(self, name: str) -> ProposalKind: ...

class Proposals(Protocol):          # proposals
    async def create(self, project_id: str, agent: str, kind: str, payload: dict[str, Any], approval_id: str, turn_id: str) -> ProposalView: ...
    async def get_by_approval(self, project_id: str, approval_id: str) -> ProposalView | None: ...
    async def pending(self, project_id: str, kind: str, key: str) -> ProposalView | None: ...   # superseding is by kind and key; key = the project for connections and handoff
```

- proposals registers an `ApprovalObserver`: on `decided` it marks the proposal, applies it on approval, and deletes its credential slots on reject, cancel or expiry. A newer proposal of the same kind cancels the pending one (`Approvals.cancel`). A second proposal of the same kind in one model step gets a tool error. At startup, an approved proposal with no `result` is applied again, and orphaned slots are deleted.
- Routes: `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}` (keystore only; refused unless pending); `GET .../proposals/by-approval/{apr}`. Events `proposal.created`, `proposal.decided`, `handoff.approved`.

### I20: the current handoff (`proposals/handoffs.py`; proposals)

```python
class HandoffView(BaseModel): proposal_id: str; approved: datetime; payload: HandoffPayload
class Handoffs(Protocol):
    async def current(self, project_id: str) -> HandoffView | None: ...
    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> None: ...   # a new version in handoff_versions
```

- Route: `GET /api/projects/{id}/handoff` returns `HandoffView` or `null` (200). Stage 4 reads `operations` from it: the baseline inventory.

### I21: key failover (`providers/failover.py`; rate-limits task 1)

```python
KeyStatus = Literal["ok", "limited", "exhausted_daily", "billing", "rejected"]
class KeyState(BaseModel): key_index: int; status: KeyStatus; since: datetime | None

class AllKeysExhausted(Exception): keys: list[KeyState]
class RateLimited(Exception): retry_after_s: float           # raised instead of waiting when wait=False
class StreamedTextFailure(Exception): cause: BaseException    # raised by an attempt after text has streamed; never failed over

class KeyFailover:   # one per app (build_services), shared by every call; state per (provider id, key index, model)
    async def call[T](self, provider: Provider, model: str, attempt: Callable[[int | None], Awaitable[T]], *, wait: bool = True) -> T: ...
```

- `attempt(key_index)` raises the provider's raw exception; `KeyFailover` classifies it with `providers/errors.classify`. A provider with no keys gets `attempt(None)` once, with no failover.
- The rules are the spec's §2. A wait round lasts at most until the earliest key clears, never more than 60 seconds.

## Files shared by these plans

Each file below is edited by one plan only, except where a line says otherwise:
- `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`, `ids.py`, `config.py`, `credentials/project_secrets.py`, `agents/approvals/service.py`, `agents/approvals/store*`, the event type lists: contracts. A later plan that needs a change opens a Decision.
- `agents/runtime/model_caller.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/approvals/waiting.py`, `providers/errors.py`, `providers/failover.py`: rate-limits.
- `providers/models.py`, `providers/service.py`, `providers/verification.py`: embedders.
- `agents/runtime/agent.py` (the injection point), `agents/catalog.py`: data-summary (onboarding later removes the `status_board` field from `catalog.py`).
- `sandbox/containers.py`, `sandbox/templates/*`: connections.
- `agents/tools/__init__.py` (`default_registry`) and `api/services.py`: each plan adds only its own lines.
- `agents/definitions/*.md`: onboarding only.
- `tests/fakes/fake_openai.py`: embedders (`/embeddings`), rate-limits (429 and 402 scripts) and onboarding (its scripted models), each in its own functions.
- `frontend/src/pages/agents/ApprovalCard.tsx`: contracts (the dispatcher and `ApprovalControls`); rate-limits (the stall card) and proposals (the two proposal cards) each add their own component file and one line in the dispatcher.
- `frontend/e2e/flow.spec.ts`: onboarding only.
- Pages: each page's `route.tsx` carries its own nav entry, so the route table and `Sidebar.tsx` need no edit.
