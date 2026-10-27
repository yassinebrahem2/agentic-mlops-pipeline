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

| Plan | Branch | Covers | Consumes (besides merged code) | Starts |
|---|---|---|---|---|
| contracts | `plan/stage-3-contracts` | I13 to I19 in code; the stage 3 schema (one migration per database); the `propose` risk class in the policy; the `stall` approval kind and part | none | now |
| rate-limits | `plan/stage-3-rate-limits` | §2: key failover per key and model, the error classification, the stall (model node, graph routing, card, Try again), the cooldown setting | I13 (stall), I19 | task 1 now (failover engine, pure); the rest after contracts merges |
| embedders | `plan/stage-3-embedders` | §3: model types, the embedder verify step, type-filtered pickers, the embedder setting, the fake `/embeddings`, `Embedder` | I14 | after contracts task 1 |
| connections | `plan/stage-3-connections` | §4 and §5: the connection registry and API, the `local_folder` kind, the settings section, the read-only mounts and container fingerprint, DuckDB in the environment image, the host-loopback test | I15 | after contracts task 1 |
| data-summary | `plan/stage-3-data-summary` | §6: the hook registry and the hook point, `status_board` as a hook, `data_summary` with the DuckDB lock-down, the result cache | I15, I18 | after contracts merges |
| tracking | `plan/stage-3-tracking` | §7: tracking sources and API, the MLflow and GitHub Actions adapters, the fake MLflow server, the sync loop, `TrackingHistory`, `tracking_summary` | I16, I18 | after contracts merges |
| knowledge | `plan/stage-3-knowledge` | §8: notes, the `VectorStore` (FTS5 + sqlite-vec), `SaveNote`, `SearchNotes`, the `knowledge` hook, re-embedding | I14, I17, I18 | after contracts merges and embedders task 1 |
| proposals | `plan/stage-3-proposals` | §9 (the mechanism): `Propose`, proposal records, superseding, the credential endpoint and its lifecycle, the cards for both kinds | I13, I19 | after contracts merges |
| onboarding | `plan/stage-3-onboarding` | §9 (applying both kinds), §10, §11: the wizard step, `connections` and `handoff` apply, the kickoff, the seven definitions, the end-to-end test | every plan above merged | last |
| pages | `plan/stage-3-pages` | §12: Models, Data and Runs pages, their routes' frontend, the sidebar entries | the tracking and connections routes on `main` | after tracking and connections merge their routes |

Owners claim plans as `AGENTS.md` says. contracts first: other plans start on its early PRs.

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

class Embedder(Protocol):
    address: str
    dimensions: int
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...   # raises EmbedderUnavailable when every key is exhausted

async def project_embedder(project_id: str) -> Embedder | None: ...   # the project override, else the app default, else None
```

### I15: connections (`connections/contract.py`)

```python
ConnectionStatus = Literal["ok", "unreachable"]
class TableRef(BaseModel): name: str; files: list[str]; format: Literal["csv", "parquet", "json", "jsonl"]
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

Routes: `GET/POST /api/projects/{id}/connections`, `DELETE /api/projects/{id}/connections/{con}`, `GET /api/projects/{id}/connections/{con}/tables`. Events `connection.created`, `connection.deleted`.

### I16: tracking (`tracking/contract.py`)

```python
SourceType = Literal["mlflow", "github_actions"]
class TrackingSourceView(BaseModel): id: str; type: SourceType; settings: dict[str, Any]; auth: Literal["none", "credential"]; credential_set: bool; last_sync: datetime | None; last_error: str | None
class ModelVersionRow, RunRow, MetricPoint, DeploymentRow, CiRunRow   # the record of §7, each with source_id and source_key
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
class NoteView(BaseModel): id: str; type: NoteType; title: str; text: str; source: str | None; author: str; created: datetime; updated: datetime
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

The registry replaces `KNOWN_HOOKS` in `agents/catalog.py`; the catalog validates `context_hooks` against `registry.names()`. The agent runtime's one injection point (today the status summary in `agents/runtime/agent.py`) calls `HookRegistry.run`; `status_board` is the first registered hook and `AgentDefinition.status_board` is removed (data-summary plan).

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
class ConnectionsPayload, HandoffPayload    # exactly §9's fields, in contracts
```

Route: `PUT /api/projects/{id}/proposals/{prp}/credentials/{slot}` (keystore only; refused unless the proposal is pending). Events `proposal.created`, `proposal.decided`, `handoff.approved`.

## Files shared by these plans

Each file below is edited by one plan only, except where a line says otherwise:
- `storage/migrations/app/0002_stage3.sql`, `storage/migrations/project/0004_stage3.sql`, `storage/tables.py`: contracts. A later plan that needs a column opens a Decision.
- `agents/runtime/model_caller.py`, `providers/errors.py`, `agents/runtime/graph.py`: rate-limits.
- `agents/runtime/agent.py` (the injection point), `agents/catalog.py`: data-summary.
- `sandbox/containers.py`, `sandbox/templates/*`: connections.
- `agents/tools/__init__.py` (`default_registry`) and `api/services.py`: each plan adds only its own lines.
- `agents/definitions/*.md`: onboarding only.
- `tests/fakes/fake_openai.py`: embedders (`/embeddings`) and rate-limits (429 and 402 scripts), each its own functions.
- `frontend/e2e/flow.spec.ts`: onboarding only.
- Frontend: each plan owns its own page or settings section files; `Sidebar.tsx` and the route table gain one line per page (pages).
