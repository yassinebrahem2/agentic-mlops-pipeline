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
| rate-limits | `plan/stage-3-rate-limits` | §2: the key failover engine (per key and model), the error classification, the stall (model node, graph routing, card, Try again), the cooldown setting | task 1 none; the rest contracts task 2 | `KeyFailover` (I21) from task 1 (early PR); the stall | task 1 now; the rest after contracts task 2 |
| embedders | `plan/stage-3-embedders` | §3: model types, the embedder verify step, type-filtered pickers, the embedder settings, the fake `/embeddings`, the `Embedders` service | I14; I21 | `Embedders` (I14) from task 1 (early PR) | after contracts task 1 and rate-limits task 1 |
| connections | `plan/stage-3-connections` | §4, §5: the registry and its routes, the `local_folder` kind, the settings section, read-only mounts and the container fingerprint, DuckDB in the environment image, the host-loopback test | I15 | `Connections` (I15) and its routes from task 1 (early PR) | after contracts task 1 |
| data-summary | `plan/stage-3-data-summary` | §6: the hook registry and the one injection point, `status_board` as a hook, `data_summary` with the DuckDB lock-down, the cache, the table-detail route | I15, I18 | `HookRegistry` (I18) from task 1 (early PR); `GET .../connections/{con}/tables/{table}` | after contracts task 1 and connections task 1 |
| tracking | `plan/stage-3-tracking` | §7: sources and routes, the MLflow and GitHub Actions adapters, the fake MLflow server, the sync loop, `TrackingHistory`, `tracking_summary` | I16, I18 | `Tracking` (I16) and its routes from task 1 (early PR) | after contracts task 1 and data-summary task 1 |
| knowledge | `plan/stage-3-knowledge` | §8: notes and routes, the `VectorStore` (FTS5 + sqlite-vec), `SaveNote`, `SearchNotes`, the `knowledge` hook, re-embedding | I14, I17, I18 | `Knowledge` (I17) | after contracts task 1, embedders task 1 and data-summary task 1 |
| proposals | `plan/stage-3-proposals` | §9's mechanism: `Propose`, proposal records, superseding, the credential slots and their lifecycle, the `Handoffs` store and route, both cards | I13, I19, I20 | `Proposals` (I19) and `Handoffs` (I20) from task 1 (early PR) | after contracts task 2 |
| onboarding | `plan/stage-3-onboarding` | §9 applying both kinds, §10, §11: the wizard step, `connections` and `handoff` apply, the kickoff, the seven definitions (and removing `status_board:`), the end-to-end test | every plan above | the working flow | after every plan above merges |
| pages | `plan/stage-3-pages` | §12: Models, Data and Runs pages, their sidebar entries | the tracking, connections, table-detail and handoff routes | three pages | after those routes are on `main` |

**contracts' tasks:** task 1 (early PR): every model and protocol of I13 to I20, and both migrations (`storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`) with `tables.py`. Task 2 (early PR): the `propose` class in the policy, the `stall` kind in the approvals store and parts (`kind`, a null `tool_call_id`, keyed by turn and step), and the approval-card dispatcher. Task 3: the regenerated `openapi.json` and frontend types for everything above.

Owners claim plans as `AGENTS.md` says; contracts first, since every other plan starts on its early PRs.

## Interfaces between plans

contracts writes these into code exactly as here; changing one takes a Decision issue. Names in `snake_case` are fields; types are pydantic models unless marked Protocol.

### I13: approvals and parts (changes to I9 and the approvals contract)

- `ApprovalKind = Literal["tool", "stall"]`; the approval record and `ApprovalView` gain `kind` (existing rows `tool`). A `stall` approval has no tool call: `tool_call_id` is null and it is keyed by `(turn_id, step)`.
- `ApprovalPart` gains `kind: ApprovalKind`; its `preview` for a stall is `Preview(kind="text", title="Stalled: no usable key", body=<per-key states>)`.
- Events `turn.stalled {turn_id, provider, model}` and `turn.resumed {turn_id}` join the event types; a stall writes no audit record.
- The policy gains the class `propose`, `ask` at every level, `bypass` included.

### I14: model types and the embedder (`providers/`)

```python
ModelType = Literal["chat", "embedder"]
# ModelEntry gains: model_type: ModelType; dimensions: int | None   (embedders, set by verify)
# ModelCreate gains: model_type: ModelType = "chat"

class EmbedderUnavailable(Exception): ...   # every key exhausted, or the provider unreachable; the message is user-facing

class Embedder(Protocol):
    address: str
    dimensions: int
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...   # raises EmbedderUnavailable

class Embedders(Protocol):            # injected into the services that need it, never looked up
    async def for_project(self, project_id: str) -> Embedder | None: ...   # the project override, else the app default, else None
```

### I15: connections (`connections/contract.py`)

```python
ConnectionStatus = Literal["ok", "unreachable"]
class TableRef(BaseModel): name: str; files: list[str]; format: Literal["csv", "parquet", "json", "jsonl"]
class ColumnInfo(BaseModel): name: str; type: str
class TableDetail(BaseModel): table: TableRef; columns: list[ColumnInfo]; row_count: int; data_version: str; summary: str   # data-summary's cached output for one table
class Mount(BaseModel): host_path: Path; container_path: str        # always read-only
class DuckDbSource(BaseModel): sql_from: str; allowed_directories: list[Path]   # the FROM clause, and what the lock-down allows
class ConnectionView(BaseModel): id: str; name: str; kind: str; settings: dict[str, Any]; status: ConnectionStatus; status_detail: str | None; secret_files: list[str]
class ConnectionKind(Protocol): ...        # exactly §4's interface
class Connections(Protocol):
    async def list(self, project_id: str) -> list[ConnectionView]: ...
    async def create(self, project_id: str, kind: str, name: str, settings: dict[str, Any]) -> ConnectionView: ...
    async def delete(self, project_id: str, connection_id: str) -> None: ...
    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]: ...
    async def data_version(self, project_id: str, connection_id: str, table: str) -> str: ...
    async def duckdb_source(self, project_id: str, connection_id: str, table: str) -> DuckDbSource: ...
    async def mounts(self, project_id: str) -> list[Mount]: ...
```

Routes: `GET/POST /api/projects/{id}/connections`, `DELETE /api/projects/{id}/connections/{con}`, `GET /api/projects/{id}/connections/{con}/tables` (connections); `GET /api/projects/{id}/connections/{con}/tables/{table}` returning `TableDetail` (data-summary). Events `connection.created`, `connection.deleted`.

### I16: tracking (`tracking/contract.py`)

```python
SourceType = Literal["mlflow", "github_actions"]
class TrackingSourceView(BaseModel): id: str; type: SourceType; settings: dict[str, Any]; auth: Literal["none", "credential"]; credential_set: bool; last_sync: datetime | None; last_error: str | None
class ModelVersionRow(BaseModel): source_id: str; model: str; version: str; run_key: str | None; aliases: list[str]; created: datetime
class RunRow(BaseModel): source_id: str; key: str; experiment: str; status: str; start: datetime | None; end: datetime | None; params: dict[str, str]; tags: dict[str, str]; git_commit: str | None; datasets: list[str]
class MetricPoint(BaseModel): source_id: str; run_key: str; metric: str; step: int; value: float; time: datetime
class DeploymentRow(BaseModel): source_id: str; model: str; alias: str; from_version: str | None; to_version: str; seen_at: datetime
class CiRunRow(BaseModel): source_id: str; key: str; workflow: str; status: str; conclusion: str | None; start: datetime; duration_s: float | None; commit: str; url: str
class SyncResult(BaseModel): versions: list[ModelVersionRow]; runs: list[RunRow]; metrics: list[MetricPoint]; aliases: dict[str, dict[str, str]]; ci_runs: list[CiRunRow]; error: str | None   # aliases: model -> alias -> version; Tracking turns changes into DeploymentRows
class TrackingQuery(BaseModel): what: Literal["models", "versions", "runs", "metrics", "deployments", "ci_runs"]; model: str | None = None; run_key: str | None = None; metric: str | None = None; since: datetime | None = None; limit: int = 50
class TrackingResult(BaseModel): rows: list[dict[str, Any]]; total: int; truncated: bool
class Tracking(Protocol):
    async def sources(self, project_id: str) -> list[TrackingSourceView]: ...
    async def create_source(self, project_id: str, type: SourceType, settings: dict[str, Any], auth: Literal["none", "credential"]) -> TrackingSourceView: ...
    async def sync(self, project_id: str, source_id: str | None = None) -> None: ...
    async def query(self, project_id: str, q: TrackingQuery) -> TrackingResult: ...   # what TrackingHistory and the pages read
class TrackingAdapter(Protocol):
    type: SourceType
    async def sync(self, source: TrackingSourceView, credential: str | None, since: datetime | None) -> SyncResult: ...
```

Routes: `GET/POST /api/projects/{id}/tracking/sources`, `POST /api/projects/{id}/tracking/sync`, `GET /api/projects/{id}/tracking/{models|runs|ci-runs|deployments}`, `PUT /api/projects/{id}/tracking/sources/{trk}/credential` (writes the keystore only). Event `tracking.synced`.

### I17: knowledge (`knowledge/contract.py`)

```python
NoteType = Literal["fact", "decision", "incident", "runbook", "doc"]
class NoteDraft(BaseModel): type: NoteType; title: str; text: str; source: str | None = None
class NoteView(BaseModel): id: str; type: NoteType; title: str; text: str; source: str | None; author: str; created: datetime; updated: datetime
class SearchResult(BaseModel): notes: list[NoteView]; used_vectors: bool; detail: str | None   # detail says why vectors were not used
class VectorStore(Protocol):
    async def upsert(self, project_id: str, note_id: str, vector: list[float], embedder: str) -> None: ...
    async def nearest(self, project_id: str, vector: list[float], embedder: str, k: int) -> list[str]: ...
    async def drop_embedder(self, project_id: str, embedder: str) -> None: ...
class Knowledge(Protocol):
    async def save(self, project_id: str, author: str, note: NoteDraft, note_id: str | None = None) -> NoteView: ...
    async def search(self, project_id: str, query: str, type: NoteType | None, limit: int) -> SearchResult: ...   # SearchResult says whether vectors were used
    async def delete(self, project_id: str, note_id: str) -> None: ...
```

Routes: `GET/POST /api/projects/{id}/notes`, `PATCH/DELETE /api/projects/{id}/notes/{kno}`.

### I18: context hooks (`agents/hooks/contract.py`)

```python
class HookContext(BaseModel): project_id: str; agent: str; trigger_text: str
class ContextHook(Protocol):
    name: str
    async def render(self, ctx: HookContext) -> str: ...   # capped text; raises nothing (a failure is one line)
class HookRegistry: def register(self, hook: ContextHook) -> None; def names(self) -> frozenset[str]; async def run(self, names: Sequence[str], ctx: HookContext) -> str
```

The registry replaces `KNOWN_HOOKS` in `agents/catalog.py`; the catalog validates `context_hooks` against `registry.names()`. The agent runtime's one injection point (today the status summary in `agents/runtime/agent.py`) calls `HookRegistry.run`; `status_board` is the first registered hook. Until onboarding rewrites the definitions, the catalog treats `status_board: true` as the `status_board` hook (data-summary); onboarding then removes the field from every definition and from `AgentDefinition`.

### I19: proposals (`proposals/contract.py`)

```python
ProposalStatus = Literal["pending", "approved", "rejected", "superseded"]
class ProposalView(BaseModel): id: str; kind: str; payload: dict[str, Any]; status: ProposalStatus; agent: str; approval_id: str; created: datetime
class ProposalKind(Protocol):
    name: str                                    # "connections" | "handoff"
    payload_model: type[BaseModel]
    def preview(self, payload: BaseModel) -> Preview: ...
    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]: ...   # slot index + label
    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult: ...   # parts applied / failed with reasons
class CredentialSlot(BaseModel): index: int; label: str; source_type: str
class ApplyResult(BaseModel): applied: list[str]; failed: dict[str, str]   # part -> reason
class ConnectionsPayload, HandoffPayload    # exactly §9's fields (HandoffPayload.operations.checks: check_id, name, category, cadence, results: file | ci | mlflow), in contracts
class Proposals(Protocol):
    async def create(self, project_id: str, agent: str, kind: str, payload: dict[str, Any], approval_id: str) -> ProposalView: ...
    async def decide(self, project_id: str, proposal_id: str, approved: bool) -> ApplyResult | None: ...   # called by the approvals path on decision
    async def pending(self, project_id: str, kind: str) -> ProposalView | None: ...
```

Route: `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}` (keystore only; refused unless the proposal is pending). Events `proposal.created`, `proposal.decided`, `handoff.approved`.

Credentials are written in one place: `credentials/project_secrets.py` (`put`, `move`, `delete` over `KeyStore`, refs `project/<id>/proposal/<prp>/<slot>` and `project/<id>/tracking/<trk>`), used by both the proposal slot route and the tracking source credential route.

### I20: the current handoff (`proposals/handoffs.py`)

```python
class HandoffView(BaseModel): proposal_id: str; approved: datetime; payload: HandoffPayload
class Handoffs(Protocol):
    async def current(self, project_id: str) -> HandoffView | None: ...
    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> None: ...   # versioned; called by the handoff kind's apply
```

Route: `GET /api/projects/{id}/handoff`. Stage 4 reads `operations` from here (the baseline inventory).

### I21: key failover (`providers/failover.py`)

```python
class KeyFailover:   # one per app, shared by every call; state per (provider, key index, model)
    async def call[T](self, provider: ProviderView, model: str, attempt: Callable[[int], Awaitable[T]]) -> T: ...   # attempt(key_index); raises AllKeysExhausted
class AllKeysExhausted(Exception): keys: list[KeyState]   # per key: state ("exhausted_daily" | "billing" | "rejected") and since
```

## Files shared by these plans

Each file below is edited by one plan only, except where a line says otherwise:
- `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`: contracts. A later plan that needs a column opens a Decision.
- `agents/runtime/model_caller.py`, `providers/errors.py`, `agents/runtime/graph.py`: rate-limits.
- `agents/runtime/agent.py` (the injection point), `agents/catalog.py`: data-summary.
- `sandbox/containers.py`, `sandbox/templates/*`: connections.
- `agents/tools/__init__.py` (`default_registry`) and `api/services.py`: each plan adds only its own lines.
- `frontend/src/pages/agents/ApprovalCard.tsx`: contracts adds a dispatcher on the approval's `kind` (and on the proposal kind); rate-limits (the stall card) and proposals (the two proposal cards) each add their own component file and one line in the dispatcher.
- `agents/definitions/*.md`: onboarding only.
- `tests/fakes/fake_openai.py`: embedders (`/embeddings`) and rate-limits (429 and 402 scripts), each its own functions.
- `frontend/e2e/flow.spec.ts`: onboarding only.
- Frontend: each plan owns its own page or settings section files; `Sidebar.tsx` and the route table gain one line per page (pages).
