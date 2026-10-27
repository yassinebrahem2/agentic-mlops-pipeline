# Stage 3: Onboarding and data access

Date: 2026-10-27. Status: draft, not approved.
Contracts used here (IDs, folder layout, SQLite rules, agent definition format, risk classes, event and audit records) are in `docs/roadmap.md`; this spec changes them there first where it says so. Background and the decisions behind it: `project_knowledge.md` (entries of 2026-10-27). Stage 1 and stage 2 behavior not mentioned here stays as their specs say.

## Goal

A user adds an ML project and, after one conversation with the Onboarding agent, the app knows the project the way a new MLOps engineer would after their first week:
- what the model is for and how its quality is judged;
- where its data lives, and which tables hold training data, live inputs, predictions and labels;
- where its runs and model versions are tracked;
- how its pipeline is built, scheduled and deployed;
- what monitoring the team already has, and where its results land.

The user approves that picture (the handoff). The app gives the other agents what they need and starts their first audits: Data Quality, Model Performance, Retraining and Deployment & Rollback each study their part and report what is good, broken or missing.

Principles (`project_knowledge.md`):
- The app is an automated coding agent specialised in MLOps, an extension of the team's system, never its production runtime. Whatever must keep running lives in the team's repository and runs on the team's scheduler; agents deliver it by PR.
- Anything an MLOps engineer does, an agent can do, behind the permission gates.
- A standalone tool needs a real reason: it reads or writes the app's own database, needs a secret the sandbox must not hold, or needs its own approval. Otherwise Bash is enough.
- Agents never load whole datasets into their context.
- Rate limits are the provider's business: a turn never stalls unless every key is exhausted.

## Scope

In:
- Turns that survive rate limits (§2), a change to the merged stage 1 runtime.
- Model types in the AI providers registry: chat models and embedders, from any provider.
- Data connections: a registry per project, read-only, with one connection kind in this stage, local folder (CSV, Parquet and JSON files). More kinds are one file each.
- Data in the sandbox: each connection mounted read-only into the agent's container, with DuckDB installed there.
- Context hooks, with three new ones: data summary, tracking summary, knowledge.
- Tracking: one record of models, versions, runs, metrics, deployments and CI runs, filled by two adapters (MLflow, GitHub Actions), with a read tool.
- The Knowledge store: notes with hybrid search, a save tool and a search tool.
- Proposals: one tool and one approval path for structured proposals; stage 3 has two kinds, `connections` and `handoff`.
- Onboarding: the add-project wizard step, the two proposals, the kickoff of four agents.
- Updated definitions for all seven agents.
- UI: the wizard step, the connections section, the proposal cards, the stalled-turn card, the embedder setting, and thin Models, Data and Runs pages.

Out:
- Connection kinds other than local folder: PostgreSQL first, then S3, GCS, Azure, MySQL, SQLite files and others. The kind interface (§4) takes each as one new file; none ships now. Excel files: they need a DuckDB extension shipped per platform.
- File connections in server mode: they would read the server's disk. The connections section says so in server mode.
- Trackers other than MLflow; a separate git commits and tags adapter (lineage comes from the git commit MLflow records on each run).
- Network rules finer than on or off (as in stage 2).
- Reading monitoring results and checks (stage 4), deploy hooks (stage 5), the Knowledge, Stats, Activity and Usage pages (stage 6). Stage 3 stores what they read.
- The Data page's quality and drift history: empty until stage 4; the page says so.

## Answers to the roadmap's stage 3 questions

- DuckDB per connection kind, and where none does: §4 (local folder uses DuckDB's built-in CSV, Parquet and JSON readers; every other kind is out of this stage and is one file each later).
- The exact command set and its caps: there is no data-command tool; agents explore with Bash in the sandbox (§5), and the app's own summary is the `data_summary` hook with its caps (§6).
- The tracking data model and the MLflow mapping: §7.
- How Onboarding's proposals are stored and approved: §9 (proposals). Onboarding proposes connections and the handoff, not tests: checks are proposed by the specialist agents after the handoff (stage 4).
- What the Knowledge store holds and who writes it: §8.

## 1. Module layout

```
backend/src/mlops_agents/
  providers/            (stage 1) gains model types, the embedder verify step, the key failover engine
  connections/          the connection registry, connection kinds (one file each), data versions
  data/                 the data-summary hook: DuckDB on the host, caps, the result cache
  tracking/             tracking sources, the record, the MLflow and GitHub Actions adapters, the sync loop
  knowledge/            notes, the VectorStore interface (sqlite-vec + FTS5), embedding
  proposals/            proposal records, kinds (one file each) and their payload models, applying
  onboarding/           the two Onboarding proposal kinds, the kickoff
  agents/
    hooks/              the context-hook registry and its hooks
    tools/              gains SaveNote, SearchNotes, TrackingHistory, Propose
    definitions/        the seven files, updated
  sandbox/              (stage 2) gains connection mounts and DuckDB in the environment image
```

## 2. Turns that survive rate limits

A change to the merged model call (`agents/runtime/model_caller.py`), modeled on the key failover engine of the Multimodal RAG Agent project (`providers/retry_engine.py` there):
- **Error kinds.** Permanent (bad request, a rejected tool call already handled in stage 2) ends the call at once. Transient (a server error, a dropped connection, a timeout) retries with backoff a few times on the same key; a different key would not fix it. Every 429 is a rate limit, handled below, whatever its message says: providers such as Gemini word per-minute limits as "quota exceeded". Only an explicit billing signal (HTTP 402, `insufficient_quota`) or an authentication failure exhausts a key at once. The classification in `providers/errors.py` changes accordingly.
- **Rate limits.** The call waits `max(retry_after, interval)` and retries the same key. Rate-limit retries never count against any retry budget, so a per-minute limit never ends a turn. When a key has been continuously rate-limited for longer than a threshold (a provider setting, default 90 seconds), it is treated as exhausted for the day.
- **Key state is shared.** Each key's rate-limit streak and exhausted mark live in one place per provider in the app, shared by every turn and every call (the four kickoff audits share a key). A success on a key ends its streak.
- **Exhausted keys.** An exhausted key is skipped and the call moves to the next key of the provider. After a cooldown (a provider setting, default 15 minutes) the next call tries it once again, since providers reset their daily limits at different times; a success clears the mark. Try again (below) clears every mark at once.
- **Every key exhausted.** The turn does not fail: it pauses on an approval of a new kind, `stall`, which has no tool call.
  - The model node records the `stall` approval (keyed by turn ID and model-step index, so a re-run finds it instead of duplicating it) and returns without a model message. The graph routes model → wait while a stall is pending; once it is approved, `wait` routes back to `model`, which calls the model again. A change to the merged turn graph.
  - The approval record, the card, the Inbox entry, surviving a restart and resuming are stage 2's. The card shows the provider and each key's state and why (daily limit, billing, rejected), with Try again (approves: clears the marks and resumes, so no work is lost) and Stop (cancels: the turn ends `stopped`). A change to the merged approvals and Part interfaces: the approval kind `stall` and its preview.
  - A stall is not a policy decision: it writes no audit record. It records `turn.stalled` and `turn.resumed` events.
  - If text has already streamed in the step when the keys run out, the turn ends `failed` as today (a half-streamed answer cannot be resumed cleanly), with the same per-key explanation and a Try again that sends the trigger message again.
- Stop works at any moment, including while a call is waiting out a rate limit.
- The app never throttles or queues agents to avoid rate limits.

## 3. Model types and embedders

- A model entry gains `model_type: "chat" | "embedder"` (named so, because providers already have a `kind`), chosen when the model is added; existing entries become `chat`. The verify step depends on the type:
  - chat: as in stage 1 (tool calling, streaming);
  - embedder: one call to the provider's OpenAI-compatible `/embeddings` endpoint with a short text; it records the vector's `dimensions`.
- `usable` is per type: a chat model needs tool calling and streaming; an embedder needs a verified dimension. Every model picker filters by type; the per-agent model defaults list chat models only.
- Any provider, hosted or self-hosted (Ollama, vLLM, LM Studio, a llama.cpp server, anything OpenAI-compatible), can serve embedders. No provider list is built in. Embedding calls use the same key failover (§2), but never stall: when the embedder's keys are exhausted, search falls back to full text and says so, and a saved note waits for its vector until the background re-embed fills it in.
- Settings gain an embedder (app default, project override), used by the Knowledge store. With none set, search is full-text only and says so.
- The fake OpenAI server gains `/embeddings` (deterministic vectors).

## 4. Data connections

**The record.** A connection belongs to one project: ID `con`, a name unique in the project (`hotel-data`), its kind, its settings (per kind), a credential reference in the keystore when the kind needs one, and its status (`ok`, `unreachable` with the reason). Stored in `project.db`.

**Kinds.** One file per kind under `connections/kinds/`, one interface:

```python
class ConnectionKind(Protocol):
    name: str                                       # "local_folder"
    settings_model: type[BaseModel]
    needs_credential: bool
    def validate(self, settings, credential) -> None: ...         # raises with a user-facing reason
    def tables(self, settings) -> list[TableRef]: ...             # what a table is for this kind
    def data_version(self, settings, table) -> str: ...           # files: size + mtime; a database: row count + latest timestamp
    def duckdb_source(self, settings, table) -> DuckDbSource: ... # how DuckDB reads it
    def sandbox_mount(self, settings) -> Mount | None: ...        # a read-only bind mount, or None for a network kind
```

- `local_folder`: settings `{path}`. Its tables: each top-level file in a supported format, and each direct subfolder whose files all share one supported format (read as one table, as `bookings/*.parquet`). Supported formats: CSV, Parquet, JSON and JSON Lines (`.jsonl`). Other files are listed as unsupported. A table's data version is its files' sizes and modified times.
- Nothing outside `connections/kinds/` knows a connection is a folder. A PostgreSQL kind later implements the same methods (a read-only ATTACH with a secret from the keystore; no mount; reached over the network).

**Rules:**
- Only the user creates, changes or deletes a connection: in project settings, or by approving a `connections` proposal (§9). No agent tool creates one.
- A local folder must exist and be readable, and must not be inside the app's data folder. Links are resolved before the check.
- Files in it that match the secret denylist are listed as a warning in the form and the proposal card. A read-only mount cannot hide them; the user decides.
- The UI lists only the kinds that ship.

## 5. Data in the sandbox

- Whether a task container can reach a service on the host bound to 127.0.0.1 (Docker Desktop on Windows, `host.docker.internal`, network on) is measured by a named test in this stage's sandbox plan; the demo's MLflow address follows from it.
- Each connection with a mount is bind-mounted read-only into every task container of the project at `/data/<connection name>`, next to the worktree. A container's fingerprint gains its mounts, so changing connections recreates the project's containers at their next Bash call (today only the image tag and the network flag are compared).
- The environment image installs DuckDB (the Python package and the CLI) outside `/workspace`, at the app's DuckDB version: 2.0.0, from one constant.
- Agents explore data with Bash: `duckdb`, pandas, or the project's own code, on `/data/...`. Bash output stays capped as in stage 2, which keeps a whole table out of the context; the container's memory limit (4 GB) stays.

## 6. Context hooks

- The agent definition's `context_hooks` names hooks from one registry (`agents/hooks/`), replacing the empty `KNOWN_HOOKS` set of stage 1. A hook runs in the app when a turn starts and returns text added to that turn's context (never to the session's messages). A hook that fails adds one line saying so and never fails the turn.
- The status board summary of stage 2 becomes a hook (`status_board`), so there is one injection point; the definition field `status_board: true` is replaced by the hook in `context_hooks` (roadmap change).
- **`data_summary`.** For each table of each connection, computed in the app process (no Docker needed):
  - schema, row count, data version;
  - per column: null count, approximate distinct count, min and max for numbers and dates, top 5 values for text;
  - the first 20 rows.
  - Caps: 20 rows; text cut at 80 characters; 50 columns per table (the rest counted); 20 tables per connection (the rest named); one size cap for the whole hook (a constant), past which tables are listed without detail.
  - DuckDB runs locked to the connection's folder, set in this order on each connection, then locked: `autoinstall_known_extensions` and `autoload_known_extensions` off, `SET allowed_directories=[<folder>]`, `SET enable_external_access=false`, `SET lock_configuration=true` (verified on DuckDB 2.0.0: a path outside, `../` included, is refused, and the lock cannot be undone).
  - Results, never data, are cached in `projects/<id>/cache/`, keyed by connection, table, the summary's format version and the table's data version. The cache has a 500 MB cap per project and evicts the least recently used.
- **`tracking_summary`** (§7) and **`knowledge`** (§8).

## 7. Tracking

**Sources.** A tracking source is its own record, not a connection (it has no tables and no mount): ID `trk`, type (`mlflow` or `github_actions`), settings (the MLflow URL; the GitHub repository and the workflows that matter), whether it needs a credential and a keystore reference, `auth: "none" | "credential"`, and its last sync result.

**The record** (in `project.db`), written only by adapters; each row keyed by its source and its ID at the source, so a sync is idempotent:
- `tracked_models` (name), `model_versions` (version, source run, aliases, created), `tracked_runs` (run ID, experiment, status, start, end, params, tags including the git commit, dataset inputs where recorded), `run_metrics` (run, key, step, value, time), `deployments` (model, alias, from version, to version, seen at: the alias history, since MLflow keeps only current aliases), `ci_runs` (workflow, run ID, status, conclusion, start, duration, commit, URL).

**Adapters**, one file each under `tracking/adapters/`, one interface `sync(source, since) -> SyncResult`:
- **MLflow** over its REST API with the app's HTTP client (no MLflow package): experiments, runs and metric history, registered models, versions and aliases.
- **GitHub Actions** over the stage 2 GitHub client: workflow runs.
- A sync loop per project runs each source on an interval (a project setting, default 5 minutes) and on demand from the Runs page, records `tracking.synced`, and keeps the last good data when a source fails (its error is shown on the Runs page).

**For agents:**
- `tracking_summary` hook: the production model and version, its latest metrics, the last runs and CI runs; capped.
- `TrackingHistory` tool (`read`; justified: the record is in the app's database): models, versions, runs, metrics over time, deployments and CI runs, filtered, with a row cap.
- With network granted, agents may call the tracker's API from Bash. For a tracker with authentication, the sandbox gets a read-only credential only if the user grants one, so it can read and never write. For a tracker without authentication (`auth: "none"`), anyone who can reach it can change it, the agents included at `autonomous` and `bypass` levels: Onboarding reports it as a gap and proposes a PR enabling the tracker's authentication; the network grant in the handoff card says so in one sentence; every Bash command is in the audit log. If the user keeps it open, that is their choice.

## 8. The Knowledge store

**Notes** (in `project.db`): ID `kno`, type (`fact`, `decision`, `incident`, `runbook`, `doc`), title, text, source (a path, URL, run or message), author (an agent or the user), created and updated times.
- Hybrid search: FTS5 on title and text, sqlite-vec on embeddings of title and text, merged by reciprocal rank fusion, behind one `VectorStore` interface.
- Each vector row records the embedder's address and dimension; the vector table is per dimension. Changing the embedder re-embeds every note in the background; until it finishes, search is full-text only.
- Code is never stored here.

**Tools** (justified: the notes are in the app's database, and search needs the embedder's key):
- `SaveNote(type, title, text, source, note_id?)`: creates a note, or updates one by ID. Risk class `read` (it changes only the app's own notes).
- `SearchNotes(query, type?, limit?)`: the best matches with their IDs.

**`knowledge` hook:** the five notes most relevant to the turn's trigger message, capped, for every agent. Every agent may write; the user edits and deletes on the Knowledge page (stage 6) and until then through the API.

## 9. Proposals

**One tool, one record, one path.** `Propose(kind, payload)`; record ID `prp`, kind, payload (validated by the kind's pydantic model), status (`pending`, `approved`, `rejected`, `superseded`), the proposing agent, the version (a newer pending proposal of the same kind supersedes the older one).
- A proposal is a stage 2 approval: the tool call asks, the card renders the payload by its kind, the user approves or rejects with a note, the turn resumes. On approval the app runs the kind's `apply()`; the tool result tells the agent what was applied, what failed and why.
- **Secrets never pass through a proposal.** A card that needs a credential (a tracker's) has a field that posts it straight to the keystore through its own endpoint, keyed by the proposal's ID and the source's position in the payload; the approval, its preview, the audit record, the events and the checkpoint only ever hold "credential set: yes or no". Applying the proposal moves the credential to the new tracking source; rejecting it, superseding it or failing to create that source deletes it, so no orphan secret stays in the keystore.
- **New risk class `propose`:** it asks at every permission level, `bypass` included. A proposal changes what the agents may do or see, so the user always decides it. Roadmap change below.
- Kinds in this stage: `connections` and `handoff`. Stage 4 adds checks and result parsers, stage 5 deploy hooks: each a new kind file with its payload model, card and `apply()`.

**`connections` payload:** the connections to create (kind, name, settings) and why each, from what Onboarding found in the code and heard from the user. Applying creates them (§4).

**`handoff` payload** (needs the project's connections to exist):
- **Profile:** the objective in the user's words, problem type, target column, positive class, primary metric and why, operating threshold if any, how late labels arrive.
- **Data roles:** per table of the connections: `training`, `serving_inputs`, `predictions`, `labels` or `other`, with the ID column that joins predictions to labels and the time column.
- **Tracking:** the sources to create (MLflow URL and whether it needs a credential; the GitHub workflows that matter).
- **Pipeline:** the stages (name, entry file or command, inputs, outputs, order). The Dashboard draws them in stage 4.
- **Commands:** the team's train, evaluate, deploy and rollback commands where found. Stage 5 registers them as deploy hooks.
- **Operations:** the team's scheduler (what runs scheduled jobs, where it is configured), and its existing monitoring as a list of checks, each with a stable `check_id`, name, category (`data_quality`, `drift`, `model_performance`, `operational`), cadence, and where its results land: `file` (connection and path pattern), `ci` (workflow) or `mlflow` (metric key). Stage 4 reads these results; this list is the baseline.
- **Gaps:** what Onboarding could not find or set up, each with its next step (for example "predictions are not saved: PR #12 adds logging with a shared ID"; "MLflow has no authentication: PR #13 enables it").
- **Grants:** sandbox network on or off, and the permission level, which can never be set to `bypass` by a proposal.

**Applying the handoff:** stores the profile, roles, pipeline, commands and operations as the project's current handoff (versioned; a later approved handoff replaces it); creates the tracking sources (the credential is already in the keystore); sets network and level; starts the tracking sync; records `handoff.approved`; starts the kickoff. A part that fails is reported and the rest is applied.

**The kickoff.** The app posts one message to each of Data Quality, Model Performance, Retraining and Deployment & Rollback at once, authored by the system and labeled "handoff", with the handoff summary and that agent's audit brief. It is posted the way stage 2's wake-ups are (`sessions.post` with author "Handoff", the recipient itself as `from_agent`, no exchange), so it is not an agent-to-agent message: replies stay in each agent's own session, nothing wakes Onboarding, and no hop or token limit applies. The user follows the audits on the status board and in the Inbox, where an audit's requests appear. Each audit studies its part (hooks, Bash on `/data`, the tracking record, the repository, the notes), saves what it learns as notes, opens PRs or issues for fixes through the stage 2 tools and gates, and ends with a report: what is good, broken or missing, with evidence. The kickoff is recorded per agent, so a restart neither repeats nor loses it.

## 10. Onboarding

- **Wizard.** Adding a project ends with an Onboarding step: whether a verified chat model is set (required), Docker's status (without it Onboarding can read the repository but not run commands), and Start. Start posts the first message to Onboarding's session (from the user, naming the project and repository) and opens it.
- **Flow.**
  1. It reads the repository: data paths, `read_sql` calls, DVC files, MLflow URIs, schedulers (GitHub workflows, cron files, Airflow DAGs), monitoring code and its outputs, the pipeline's commands. It asks the user about the objective, the use, the metric, and where data and results live.
  2. It proposes the `connections`. Until the user approves, it reads only the repository.
  3. With the data mounted and summarized, it explores the tables, then proposes the `handoff`.
- **Missing predictions or labels.** When nothing records predictions and labels with a shared ID, Onboarding opens a PR adding that logging where a connection can read it, and lists it under Gaps.
- **No tracking.** If the team has no MLflow, Onboarding opens a PR adding MLflow tracking to the training code, and lists it under Gaps.
- Onboarding can be talked to again at any time; a newer approved proposal replaces the current one.

## 11. The seven agents

All seven definition files change: their prompts describe what each can now do (connections at `/data/<name>`, the hooks, notes, the tracking record, proposals) and nothing it cannot yet (checks and result reading in stage 4, deploy hooks in stage 5).

| Agent | New tools | Hooks |
|---|---|---|
| Orchestrator | SaveNote, SearchNotes, TrackingHistory | status_board, tracking_summary, knowledge |
| Onboarding & Diagnosis | SaveNote, SearchNotes, TrackingHistory, Propose | data_summary, knowledge |
| Data Quality | SaveNote, SearchNotes | data_summary, knowledge |
| Model Performance | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| Retraining | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| Deployment & Rollback | SaveNote, SearchNotes, TrackingHistory | tracking_summary, knowledge |
| Alert Prioritization | none (stage 4) | none |

Onboarding's `max_steps` rises to 60. Agents that had `status_board: true` get the `status_board` hook.

## 12. UI

- **Add project:** the Onboarding step (§10).
- **Project settings, Connections:** connections with status and data version; add (a folder picker in local mode, as the add-project wizard has), edit, remove; the denylist warning. Tracking sources: URL, credential (posted to the keystore), last sync. The embedder override.
- **Proposal cards:** `connections` and `handoff` cards render their payloads readably; the handoff card has the credential field and the network sentence of §7. The Inbox lists them like any approval.
- **Stalled-turn card** (§2), in the session and the Inbox.
- **AI providers:** the model type column, type-filtered pickers, the embedder default in app settings, the rate-limit threshold per provider.
- **Models page:** tracked models; per model, its versions with their metrics side by side, the production alias, and each version's lineage (run, git commit, training data where recorded).
- **Data page:** connections and their tables (schema, rows, data version, roles from the handoff), and "quality and drift history arrives with monitoring".
- **Runs page:** tracked runs and CI runs in one list with status, duration and failures; Sync now.
- The sidebar shows Models, Data and Runs only with their pages.

## 13. Storage

New tables in `project.db`: connections, tracking sources, the tracking record, notes with their FTS and vector tables, proposals, the current handoff, the kickoff state. App settings gain the default embedder; providers gain the rate-limit threshold and models the type and dimensions (`app.db`). `projects/<id>/cache/` holds the data-summary cache. Each plan adds its own numbered migration.

## Changes to the roadmap

Made in `docs/roadmap.md` before any stage 3 plan:
- ID prefixes `con` (connection), `trk` (tracking source), `prp` (proposal), `kno` (knowledge note). Tracking record rows are keyed by source and source ID (no prefix): they mirror another system's IDs.
- Audit records: a `stall` decision writes none (it is not a policy decision); events record it.
- Risk class `propose` in the classes and the policy table: `ask` at every level, `bypass` included (the one class `bypass` does not skip).
- Agent definitions: `status_board` becomes a context hook; `context_hooks` names hooks from the registry.
- Tools: data commands are not a tool; agents explore data with Bash in the sandbox. Risk class `read` lists SaveNote, SearchNotes, TrackingHistory instead of data commands.
- Data folder layout: `cache/` holds data-summary results.
- Stage 3 "Agreed" rewritten: Onboarding hands off (profile, connections, roles, tracking, pipeline, commands, operations, gaps, grants) instead of proposing tests and a monitoring plan; connection kinds (local folder now, others later, one file each); MLflow is a tracking source, not a connection kind; data understanding is Bash with DuckDB plus the `data_summary` hook, with no result handles or `read_result`.

## 14. Error handling

- An unreachable connection is marked with the reason; hooks skip it with one line; the Data page shows it.
- A tracking sync failure is recorded on its source and shown on the Runs page; the last good data stays.
- An embedder failure leaves search full-text only and says so.
- Errors from DuckDB, trackers and embedders go through the existing secret scrubbing before they are logged, stored or shown.

## 15. Testing

- Fakes, no real service: `/embeddings` and rate-limit and quota responses on the fake OpenAI server; a fake MLflow REST server; workflow runs on the fake GitHub server; small data folder fixtures.
- Rate limits (§2), with a fake clock: per-minute limits never end a turn, a 429 saying "quota" included; an exhausted key is tried again after the cooldown; an exhausted embedder degrades search and never stalls; a key limited past the threshold is skipped; quota or authentication exhausts a key at once; every key exhausted pauses the turn with the card, Try again resumes from the checkpoint, Stop ends it; Stop during a wait.
- Connections: a folder inside the data folder is refused; a link out is resolved and checked; the mount is read-only (a write from Bash fails); denylisted files are listed; changing connections recreates the container.
- `data_summary`: caps on a wide and a long table; cache hit on an unchanged table, recompute on a changed file, eviction past the cap; a path outside the connection is refused by DuckDB.
- Tracking: the MLflow mapping (runs, metric history, versions, aliases into deployments); a second sync adds nothing; one failing source does not stop the other.
- Knowledge: a keyword match and a meaning match both rank; changing the embedder re-embeds and never mixes dimensions.
- Proposals: `Propose` asks at every level, `bypass` included; a credential never appears in the approval, audit, events or checkpoint; a credential entered for a proposal that is then rejected, superseded or fails to apply is deleted from the keystore; approving applies every part; a failing part is reported and the rest applied; a newer proposal supersedes the pending one; a handoff cannot set `bypass`.
- Kickoff: four agents started at once by a system message labeled "handoff"; Onboarding is not woken by their replies; a restart neither repeats nor loses one.
- End to end (fake model, a fixture repository and data folder): add the project, Onboarding proposes connections, approve, it proposes the handoff, approve, the connection and tracking appear, a kickoff turn runs with the data summary in its context.

## Done when

- The end-to-end test above passes and CI is green on every job.
- No agent can read a host path outside a confirmed connection, write to a connection, or receive a secret through a proposal.
- Acceptance, once the demo exists (it does not gate merging): on a fresh copy of the demo company with a real model, Onboarding reaches an approvable handoff, and the four audits each end with a report and notes.

## Build order

1. Contracts: model types, the connection kind interface, tracking sources and the record, notes, proposals with both payloads, the hook registry, the `propose` class, the new IDs and tables. Other plans build on these.
2. In parallel: rate limits (§2); embedders; connections and mounts; DuckDB in the sandbox and `data_summary`; tracking adapters and the sync loop; the Knowledge store and its tools; proposals and their cards.
3. Onboarding: the wizard step, both kinds' `apply()`, the kickoff, the definitions.
4. Pages: Models, Data, Runs, the connections section.
