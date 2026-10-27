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

The user approves that picture once (the handoff). The app then gives the other agents what they need and starts their first audits: Data Quality, Model Performance, Retraining and Deployment & Rollback each study their part and report what is good, broken or missing.

Principles this stage follows (`project_knowledge.md`):
- The app is an automated coding agent specialised in MLOps, an extension of the team's system, never its production runtime. Whatever must keep running (checks, retraining, schedules) lives in the team's repository and runs on the team's scheduler; agents deliver it by PR.
- Anything an MLOps engineer does, an agent can do, behind the permission gates.
- A standalone tool needs a real reason: it reads or writes the app's own database, needs a secret the sandbox must not hold, or needs its own approval. Otherwise Bash is enough.
- Agents never load whole datasets into their context.

## Scope

In:
- Model types in the AI providers registry: chat models and embedders, from any provider.
- Data connections: a registry per project, read-only, with one kind in this stage, local folder. The design takes more kinds as one file each.
- Data in the sandbox: each confirmed connection mounted read-only into the agent's container, with DuckDB installed there.
- The data-summary context hook: a capped, cached summary of a project's tables injected into a turn.
- Tracking: one record of models, versions, runs, metrics, deployments and CI runs, filled by two adapters (MLflow, GitHub Actions), with a context hook and a read tool.
- The Knowledge store: notes agents and the user keep, with hybrid search, a save and a search tool, and a context hook.
- Proposals: one tool and one approval path for structured proposals; stage 3 uses it for the handoff.
- Onboarding and the handoff: the add-project wizard ending in Onboarding's session, the handoff record, the grants it carries, and the kickoff of four agents.
- Updated definitions for all seven agents.
- UI: the wizard step, the connections section, the handoff card, the embedder setting, and thin Models, Data and Runs pages.

Out:
- Connection kinds other than local folder: PostgreSQL first, then S3, GCS, Azure, MySQL, SQLite files and others. The kind registry (§3) is built so each is one new file; none ships now.
- File connections in server mode: they would read the server's disk. In server mode the connections section says so.
- Trackers other than MLflow (W&B and others); a separate git commits and tags adapter (lineage comes from the git commit MLflow records on each run).
- Reading monitoring results and checks (stage 4), deploy hooks (stage 5), the Knowledge, Stats, Activity and Usage pages (stage 6). Stage 3 stores what those stages read.
- The Data page's quality and drift history: empty until stage 4 produces it; the page says so.

## 1. Module layout

```
backend/src/mlops_agents/
  providers/            (stage 1) gains model kinds and the embedder verify step
  connections/          the connection registry, connection kinds (one file each), data versions
  data/                 the data-summary hook: DuckDB on the host, caps, the result cache
  tracking/             the tracking record, the MLflow and GitHub Actions adapters, the sync loop
  knowledge/            notes, the VectorStore interface (sqlite-vec + FTS5), embedding
  proposals/            proposal records, kinds and their payload models, applying an approved proposal
  onboarding/           the handoff kind, the grants it applies, the kickoff
  agents/
    hooks/              the context-hook registry and the three hooks of this stage
    tools/              gains SaveNote, SearchNotes, TrackingHistory, Propose
    definitions/        the seven files, updated
  sandbox/              (stage 2) gains connection mounts and DuckDB in the environment image
```

## 2. Model kinds and embedders

- A model entry gains `kind: "chat" | "embedder"`, chosen when the model is added. The verify step depends on the kind:
  - chat: as in stage 1 (tool calling, streaming);
  - embedder: one call to the provider's OpenAI-compatible `/embeddings` endpoint with a short text; it records the vector's `dimensions`.
- `usable` is per kind: a chat model needs tool calling and streaming; an embedder needs a verified dimension. Every model picker filters by kind; the per-agent model defaults list chat models only.
- Any provider of either kind (hosted or self-hosted: Ollama, vLLM, LM Studio, a llama.cpp server, anything OpenAI-compatible) can serve embedders. No provider list is built in.
- Settings gain an embedder (app default, project override), used by the Knowledge store. With none set, search is full-text only and the Knowledge section says so.
- Changing the embedder re-embeds the project's notes in the background (§6); vectors are never mixed across embedders.
- The fake OpenAI server gains `/embeddings` (deterministic vectors), so tests and e2e need no real provider.

## 3. Data connections

**The record.** A connection belongs to one project: ID `con`, a name unique in the project (`hotel-data`), its kind, its settings (per kind), a credential reference in the keystore when the kind needs one, and its status (`ok`, `unreachable`). Stored in `project.db`.

**Kinds.** One file per kind under `connections/kinds/`, each implementing one interface:

```python
class ConnectionKind(Protocol):
    name: str                                      # "local_folder"
    settings_model: type[BaseModel]                # validated settings
    needs_credential: bool
    def validate(self, settings, credential) -> None: ...        # raises with a user-facing reason
    def tables(self, settings) -> list[TableRef]: ...            # what a table is for this kind
    def data_version(self, settings, table) -> str: ...          # e.g. size + mtime; row count + max timestamp for a database
    def duckdb_source(self, settings, table) -> DuckDbSource: ...  # how DuckDB reads it (path and reader, or ATTACH + secret)
    def sandbox_mount(self, settings) -> Mount | None: ...       # read-only bind mount, or None for network kinds
```

- `local_folder`, the one kind of this stage: settings `{path}`. A table is one file (CSV, Parquet, JSON or Excel) or one folder of same-schema Parquet or CSV files read as one table (as `bookings/*.parquet` is). Its data version is the size and modified time of its files.
- The rest of the app (hook, cache, tracking, pages, proposals) only uses this interface; nothing outside `connections/kinds/` knows a kind is a folder. A PostgreSQL kind later implements the same methods (its data version: row count and latest timestamp; its DuckDB source: a read-only ATTACH with a secret from the keystore).

**Rules:**
- Only the user creates, changes or deletes a connection: in project settings, or by approving a proposal that lists it (§7). No agent tool creates one.
- A local folder must exist, be readable, and not be inside the app's data folder or the repository's own worktrees. Links are resolved before the check.
- The UI lists only the kinds that ship.

**In the sandbox.** Each connection of the project with a mount is bind-mounted read-only into every task container at `/data/<connection name>`, alongside the worktree. Changing connections recreates the project's task containers at their next Bash call. The secret denylist applies to the mount as to the worktree. Network stays as the project sets it.

## 4. DuckDB and the data-summary hook

**In the sandbox.** The environment image installs DuckDB (the Python package and the CLI) outside `/workspace`, at the app's own DuckDB version, from one constant. Agents explore data with Bash: `duckdb`, pandas, or the project's own tools, on `/data/...`. Bash output stays capped as in stage 2 (the head and tail of the output), which is what keeps a whole table out of the context.

**Context hooks.** The agent definition's `context_hooks` lists hook names from one registry (`agents/hooks/`). A hook runs in the app when a turn starts and returns text that is added to the turn's context (never to the session's messages). A hook that fails adds one line saying so and never fails the turn. Stage 3 registers three hooks: `data_summary`, `tracking_summary` (§5) and `knowledge` (§6).

**`data_summary`.** For each table of each connection, in the app process (no Docker needed):
- its schema (columns and types), row count, data version;
- per column: null count, distinct count (approximate), min and max for numbers and dates, top 5 values for text;
- the first 20 rows.
- Caps: at most 20 rows; text values cut at 80 characters; at most 50 columns shown per table (the rest counted); at most 20 tables per connection (the rest listed by name). The whole hook output has one size cap (a constant), past which tables are listed without detail.
- DuckDB runs on the host with external access locked to the connection's own paths (DuckDB's own settings for allowed paths and directories, set before any query, then locked; the plan names the exact settings for the pinned version and tests that a path outside the connection is refused). No extension is auto-installed or auto-loaded; the readers this stage needs are built in, except Excel, whose extension is installed from a bundled file.
- Results are cached in `projects/<id>/cache/`, keyed by connection, table, the summary's version and the table's data version, so a turn does not recompute an unchanged table. The cache has a size cap per project (500 MB) and evicts the least recently used. Results are cached, never data.
- Which agents get it: Onboarding, Data Quality, Model Performance and Retraining.

## 5. Tracking

**The record** (in `project.db`), filled only by adapters:
- `tracked_models` (name, source), `model_versions` (version, source run, aliases, created), `tracked_runs` (run ID, experiment, status, start and end, params, tags including the git commit, data inputs where the tracker records them), `run_metrics` (run, key, step, value, time), `deployments` (model, alias, version, from, to: the alias history, which is what a deployment is for an MLflow-based team), `ci_runs` (workflow, run ID, status, conclusion, start, duration, commit, URL).
- Each row keeps its source and source ID; a sync is idempotent.

**Adapters**, one file each under `tracking/adapters/`, one interface (`sync(project, since) -> SyncResult`):
- **MLflow**, over its REST API with the app's HTTP client (no MLflow package): experiments, runs and their metrics, registered models, versions and aliases. The tracking connection holds the server URL and an optional credential (keystore). Alias history is built by comparing aliases at each sync, since MLflow keeps only the current alias.
- **GitHub Actions**, over the GitHub integration of stage 2: the repository's workflow runs.
- A sync loop per project runs every adapter on an interval (a project setting, default 5 minutes) and on demand from the Runs page. It records `tracking.synced` events. A failing adapter records the error on its source and never stops the others.

**For agents:**
- `tracking_summary` hook: the production model and version, its latest metrics, the last runs and their status, the last CI runs; capped like `data_summary`. Given to Model Performance, Retraining, Deployment & Rollback and the Orchestrator.
- `TrackingHistory` tool (`read`), justified because the record lives in the app's database: query models, versions, runs, metrics over time, deployments and CI runs, with filters and a row cap.
- Calling the tracker's own API from Bash needs network and, for a server with authentication, a read-only credential the user grants. A tracking server without authentication is never exposed to the sandbox: for it, the handoff never offers the network grant (§7), and agents read through `TrackingHistory`. This keeps deploys behind deploy hooks (stage 5): the sandbox can read the tracker, never write to it.

## 6. The Knowledge store

**Notes** (in `project.db`): ID `kno`, type (`fact`, `decision`, `incident`, `runbook`, `doc`), title, text, source (a file path, URL, run or message), author (an agent or the user), created and updated times.
- Search is hybrid: FTS5 on title and text, and sqlite-vec on embeddings of title and text, merged by reciprocal rank fusion. Both sit behind one `VectorStore` interface (stage 1 agreed), so the store can change without touching callers.
- Each vector row records the embedder's address and dimension. The vector table is per dimension; a change of embedder re-embeds every note in the background and search uses full text alone until it finishes.
- Code is never stored here: agents read code with their file tools.

**Tools** (justified: the notes live in the app's database, and search needs the embedder's key):
- `SaveNote(type, title, text, source, note_id?)`: creates a note, or updates one when `note_id` is given. Risk class `read` (it changes only the app's own notes, never the project).
- `SearchNotes(query, type?, limit?)`: returns the best matches with their IDs.

**`knowledge` hook:** the notes most relevant to the turn's trigger message (top 5, capped), for every agent.

Every agent may write; the user edits and deletes on the Knowledge page (stage 6) and, until then, through the API.

## 7. Proposals and the handoff

**Proposals.** One tool, `Propose(kind, payload)`, and one record: ID `prp`, kind, payload (validated against the kind's pydantic model), status (`pending`, `approved`, `rejected`, `superseded`), the proposing agent, and the version (a new proposal of the same kind for the project supersedes the pending one).
- A proposal is an approval (stage 2's gate, records and cards): the tool call asks, the card shows the payload rendered by its kind, the user approves or rejects with a note, and the turn resumes. On approval the app applies the payload through the kind's `apply()`; the tool result tells the agent what was applied or why it was rejected.
- New risk class `propose`: it asks at every level, `bypass` included, when the payload grants the sandbox more than it has (a new mount or network); otherwise it asks at `suggest`, `ask` and `autonomous` and is allowed under `bypass`. Roadmap change below.
- Stage 3 has one kind, `handoff`. Stage 4 adds checks and result parsers, stage 5 deploy hooks; each is a new kind file with its payload model, its card and its `apply()`.

**The handoff payload:**
- **Profile:** the model's objective in the user's words, problem type, target column, positive class, primary metric and why, operating threshold if any, how late labels arrive.
- **Data:** the connections to create (kind, name, settings) and, per table, its role: `training`, `serving_inputs`, `predictions`, `labels`, `other`, with the ID column that joins predictions to labels and the time column.
- **Tracking:** the MLflow server URL and whether it needs a credential (the user enters it in the card), and the GitHub Actions workflows that matter.
- **Pipeline:** the stages (name, entry file or command, inputs, outputs, order). The Dashboard draws them in stage 4.
- **Commands:** the team's train, evaluate, deploy and rollback commands where found (stage 5 registers them as hooks).
- **Operations:** the team's scheduler (what runs scheduled jobs, where it is configured) and its existing monitoring: each check, where its results land, how often it runs.
- **Gaps:** what Onboarding could not find or set up, each with the next step (for example "predictions are not saved: PR #12 adds logging with a shared ID").
- **Grants:** sandbox network on or off, the permission level, and the connections above.

**Applying it** (one transaction for the records, then the side effects):
- stores the profile, roles, pipeline, commands and operations as the project's current handoff (versioned; a later approved handoff replaces it);
- creates the connections and stores the tracking credential in the keystore;
- sets the network and permission level;
- starts the tracking sync;
- records `handoff.approved`;
- starts the kickoff.

**The kickoff.** The app posts one message to each of Data Quality, Model Performance, Retraining and Deployment & Rollback, labeled from Onboarding, with the handoff summary and that agent's audit brief. They run one after another (the next is posted when the previous turn ends), so one rate-limited key is never hit by four turns at once. Each audit:
- studies its part with the hooks, Bash on the mounted data, the tracking record, the repository and the Knowledge store;
- saves what it learns as notes;
- ends with a report: what is good, what is broken, what is missing, each with evidence;
- opens PRs or issues for fixes, through the stage 2 tools and gates.

## 8. Onboarding

- **Wizard.** Adding a project ends with an Onboarding step: it shows whether a verified chat model is set (required), Docker's status (Onboarding can read the repository without it, but cannot run commands), and a Start button. Start posts the first message to Onboarding's session (labeled from the user, with the project's name and repository) and opens it.
- **What Onboarding does.** It reads the repository; finds data paths, `read_sql` calls, DVC files, MLflow URIs, schedulers (GitHub workflows, cron files, Airflow DAGs), monitoring code and its outputs, and the pipeline's commands; asks the user about the objective, the use, the metric and where data and results live; and proposes the handoff with `Propose`. Until the user has confirmed a connection, it explores the repository only; it never reads a path outside it.
- **Missing predictions or labels.** When nothing records predictions and labels with a shared ID, Onboarding opens a PR adding that logging to a place a connection can read, and lists it under Gaps.
- **Tracking.** If the team has MLflow, the handoff connects it; if not, Onboarding opens a PR adding MLflow tracking to the training code, and lists it under Gaps.
- Onboarding can be talked to again at any time; a new handoff proposal supersedes the current one when approved.

## 9. The seven agents

All seven definition files change: their prompts describe what each can now do (connections, data in the sandbox at `/data/<name>`, the hooks, notes, the tracking record, proposals) and nothing it cannot yet (checks and result reading arrive in stage 4, deploy hooks in stage 5).

| Agent | New tools | Hooks |
|---|---|---|
| Orchestrator | SaveNote, SearchNotes, TrackingHistory | tracking_summary, knowledge |
| Onboarding & Diagnosis | SaveNote, SearchNotes, TrackingHistory, Propose | data_summary, knowledge |
| Data Quality | SaveNote, SearchNotes | data_summary, knowledge |
| Model Performance | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| Retraining | SaveNote, SearchNotes, TrackingHistory | data_summary, tracking_summary, knowledge |
| Deployment & Rollback | SaveNote, SearchNotes, TrackingHistory | tracking_summary, knowledge |
| Alert Prioritization | none (stage 4) | none |

`max_steps` for Onboarding rises to 60: reading a repository and building a handoff takes more steps than a chat answer.

## 10. UI

- **Add project:** the Onboarding step (§8).
- **Project settings, Connections:** the project's connections, their status and data version; add (a folder picker in local mode, as the add-project wizard has), edit, remove; the tracking connection (URL, credential); the embedder override.
- **The handoff card:** an approval card whose body shows each section of the payload readably, with the credential field for the tracker; the Inbox lists it like any approval.
- **AI providers:** the kind column, kind-filtered pickers, the embedder default in app settings.
- **Models page:** tracked models; per model, its versions with their metrics side by side, the production alias, and each version's lineage (run, git commit, training data where recorded).
- **Data page:** connections and their tables (schema, rows, data version, roles from the handoff); "quality and drift history arrives with monitoring" until stage 4.
- **Runs page:** tracked runs and CI runs in one list, with status, duration and failures; a Sync now button.
- The sidebar shows Models, Data and Runs only with their pages, as stage 1's rule says.

## 11. Storage

New tables in `project.db` (one migration per plan that adds them): connections, data versions seen, the tracking record (§5), notes and their FTS and vector tables, proposals, the current handoff. App settings gain the default embedder. `projects/<id>/cache/` holds the data-summary cache.

## Changes to the roadmap

Made in `docs/roadmap.md` before any stage 3 plan:
- ID prefixes `con` (connection), `prp` (proposal), `kno` (knowledge note).
- Risk class `propose` and its row in the policy table (§7), including the exception to `bypass` for grants that widen the sandbox.
- Tools: data exploration is Bash in the sandbox; data commands are not a tool. The roadmap's stage 3 bullets on result handles and `read_result` are removed (a narrower query in Bash replaces them).
- Data connection kinds: local folder in stage 3; others later, one file each.

## 12. Error handling

- A connection that becomes unreachable is marked `unreachable` with the reason; hooks skip it with one line; the Data page shows it.
- An adapter sync failure is recorded on its source and shown on the Runs page; the last good data stays.
- An embedder failure leaves search full-text only and says so in the result.
- Errors from DuckDB and the trackers pass through the existing secret scrubbing before they are logged, stored or shown.
- An approved handoff whose side effect fails (a connection that no longer validates, a tracker that rejects the credential) records which part failed, applies the rest, and the tool result tells Onboarding.

## 13. Testing

- Fakes, so no test needs a real service: `/embeddings` on the fake OpenAI server; a fake MLflow REST server (experiments, runs, metrics, registered models, aliases); the fake GitHub server gains workflow runs; small data folders as fixtures.
- Connections: a folder inside the data folder or a worktree is refused; a link out of an allowed folder is refused; the mount is read-only (a write from Bash fails).
- `data_summary`: the caps hold on a wide and a long table; an unchanged table is served from the cache; a changed file recomputes; the cache evicts past its cap; DuckDB refuses a path outside the connection.
- Tracking: MLflow mapping (runs, metrics history, versions, aliases into deployments); a second sync adds nothing; one failing adapter does not stop the other.
- Knowledge: hybrid search ranks a keyword match and a meaning match; changing the embedder re-embeds and never mixes dimensions.
- Proposals: a handoff asks at every level; with a new mount or network it asks under `bypass` too; approving applies every part; a failing part is reported and the rest applied; a newer proposal supersedes the pending one.
- Kickoff: the four agents are started one after another, each labeled from Onboarding.
- End to end (fake model): add a project with a data folder, Onboarding proposes a handoff, approve it, the connection appears, the first kickoff turn runs with the data summary in its context.
- A live check on the demo company (`hotel-cancellation-demo`, a fresh copy) with a real model before the stage is called done: Onboarding reaches an approvable handoff.

## Done when

- A user can add the demo company, talk to Onboarding, approve a handoff, and see the connections, the tracking record and the Models, Data and Runs pages filled.
- The four kickoff audits run and each ends with a report and notes.
- No agent can read a host path outside a confirmed connection, write to a connection, or reach an unauthenticated tracker from the sandbox.
- CI green on every job.

## Build order

1. Contracts: model kinds, the connection kind interface, the tracking record, notes, proposals and the handoff payload, the hook registry, the `propose` risk class, the new IDs and tables. Other plans build on these.
2. In parallel: embedders; connections and mounts; DuckDB in the sandbox and the data-summary hook; tracking adapters and the sync loop; the Knowledge store and its tools; proposals and the handoff card.
3. Onboarding: the wizard step, the handoff kind's `apply()`, the kickoff, the definitions.
4. Pages: Models, Data, Runs, the connections section.
