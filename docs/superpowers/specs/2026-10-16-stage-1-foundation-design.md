# Stage 1: Foundation and first agent

Date: 2026-10-16. Status: design approved in conversation, awaiting review of this document.
Contracts used here (IDs, folder layout, SQLite rules, agent definition format, risk classes) are defined in `docs/roadmap.md`. Background: `project_knowledge.md`.

## Goal

A user installs the app with one command, adds AI providers and a project (local folder or GitHub repo), opens the project, and chats with the Orchestrator, which reads the repo with Claude Code-style read-only tools and streams its answers live. Underneath sits the shared agent runtime that every later agent will use.

## Scope

In:
- Packaging and `mlops-agents start` (local and server modes).
- Storage: `app.db`, per-project `project.db`, migrations, backups.
- Workspace: add, open, refresh, relink, remove projects.
- AI providers registry (hosted and self-hosted), keystore, verification, model assignment.
- GitHub integration: authentication and cloning.
- Shared agent runtime and agent definition files; the Orchestrator definition.
- Tools: Read, Glob, Grep (read-only).
- UI: app shell, Projects, Add project, AI providers, Integrations (GitHub), app Settings, project Agents page (Orchestrator session), project Settings.

Out (later stages): Bash and the Docker sandbox, Write/Edit, other agents, ListAgents/SendMessage, permission policy, PRs and issues, events and audit, data connections, monitoring, Discord/email, every other page. Local model files (GGUF) are deferred with no stage assigned.

## 1. Architecture and repo layout

A modular monolith: one Python backend split into domain modules. Each module owns its data access, logic and API routes. Dependencies point one way: `api` -> domain modules -> `storage`.

```
backend/
  pyproject.toml                 uv-managed package, placeholder name mlops_agents
  src/mlops_agents/
    config.py                    app name, run mode, data folder (MLOPS_AGENTS_HOME override)
    cli.py                       `mlops-agents start [--server] [--port]`, `mlops-agents backup`
    ids.py                       prefixed ULIDs
    storage/                     engines, connection setup, migration runner, backup
      migrations/app/*.sql
      migrations/project/*.sql
    workspace/                   projects: add, open, refresh, relink, remove; folder browser
    providers/                   registry, keystore, verification, chat-model bridge
    integrations/github/         auth (gh login or token), clone and fetch
    agents/
      runtime/                   loop, turn queue, streaming, persistence, compaction, usage, limits
      tools/                     tool registry; read, glob, grep; repo file filter
      definitions/orchestrator.md
    api/                         FastAPI app, module routers, SSE, static UI mount, error format
    web/                         built frontend (generated)
  tests/
frontend/
  package.json                   Vite + React + TypeScript + Tailwind + shadcn/ui
  src/                           React Router, TanStack Query
docs/
```

Runtime: one process. `mlops-agents start` runs uvicorn with the FastAPI app, which serves the API and the built UI on one port and opens the browser (local mode only). Agent turns run as asyncio tasks in the same process. Live agent output reaches the browser over server-sent events. In development, Vite serves the UI with hot reload and proxies `/api` to the backend.

Run modes (see roadmap): `local` (default) and `server` (`--server`). Server mode hides local folders, uses the encrypted key file, and does not open a browser.

## 2. Storage and workspace

### Databases

`app.db`:
- `projects`: id, name, source (`local` | `github`), local_path (local only), remote_url and owner/repo (github only), default_branch, status (`ok` | `missing` | `clone_failed`), created_at, last_opened_at.
- `providers`, `provider_keys_meta` (masked hints and order only; secrets live in the keystore), `models`: see section 3.
- `agent_model_defaults`: agent name -> model address.
- `integrations`: GitHub auth mode (`gh` | `token`), login name, verified_at.
- `settings`: key/value app settings.

`project.db`:
- `sessions`: id, agent name, created_at, compacted_through (message id).
- `messages`: id, session_id, turn_id, author_kind (`user` | `agent` | `system`), author_name, from_agent (nullable, used from stage 2), parts (JSON: text, tool calls with inputs, tool results), status (`complete` | `streaming` | `interrupted` | `queued`), created_at.
- `turns`: id, session_id, status (`queued` | `running` | `done` | `stopped` | `interrupted` | `failed`), started_at, ended_at, error.
- `llm_calls`: id, turn_id, agent, model address, prompt_tokens, completion_tokens, latency_ms, created_at.
- `project_settings`: key/value, including per-agent model overrides.
- LangGraph checkpoint tables (created by the SQLite checkpointer).

Source of truth: the `messages` table is what the UI shows (full history, authors, labels). LangGraph checkpoints are the agent's working state (compacted context). Both are written in the same turn. Checkpoints are pruned to the latest few per session.

### Access and migrations

SQLAlchemy Core in async mode over aiosqlite. Every connection, including the checkpointer's, gets WAL, `busy_timeout` and foreign keys on. Migrations are numbered SQL files per database kind, applied when a database is opened, after an automatic backup of that file. Moving to Postgres later means changing the SQLAlchemy URL and switching to LangGraph's Postgres checkpointer.

### Workspace operations

- Add local (local mode only): a backend folder browser lists directories on the machine running the app; a typed path also works. The folder must be a git repo; if it is not, the app offers `git init`. The project points at the folder in place; nothing is copied.
- Add from GitHub: paste a URL or pick from the user's repositories. The app clones into `projects/<id>/repo/` with the default branch checked out.
- Duplicate check: the same local path (canonicalized, case-insensitive on Windows) or the same GitHub owner/repo cannot be added twice.
- Open: updates last_opened_at; for GitHub projects, fetches and fast-forwards the managed clone. The managed clone is never edited by the app.
- Refresh: same fetch and fast-forward on demand.
- Missing: a local project whose folder no longer exists, or is no longer a git repo, gets status `missing`; the user relinks it to a new path.
- Remove: deletes `projects/<id>/` and the row. The user's local repo is never touched.
- Backup: `mlops-agents backup` writes `VACUUM INTO` copies of `app.db` and every `project.db` into `backups/<timestamp>/`.

Promise to the user: the app never changes your working files or your checked-out branch. From stage 2 it adds agent branches to the repo's git data; worktrees live in the app's data folder.

## 3. AI providers

Ported from `D:\SCRIPTS\Multimodal RAG Agent` (core only): provider connections, model entries, base URL validation and credential scrubbing, keystore, verification, retry with Retry-After and key rotation. Not ported: model slots, substitution, local downloads, embedder mechanisms, axis wiring.

Provider kinds, all over the OpenAI-compatible protocol:
- `hosted`: a vendor API (Groq, Gemini, OpenRouter, ...). Base URL plus one or more keys.
- `self_hosted`: a server the user runs (Ollama, vLLM, LM Studio). Base URL; key optional.

Keys: OS keyring in local mode; `keys.enc` (encrypted with a master key from the `MLOPS_AGENTS_MASTER_KEY` environment variable) in server mode. Keys never enter a database, a URL or a log. "Detect keys" scans environment variables for known provider key names and offers to import them.

Models: address `<provider>:<wire-model-id>`; fields: traits, context window (from the provider if it reports one, otherwise entered by the user), availability (`untested` | `verified` | `needs_attention`), detail. Adding a provider lists its models via `/models`, or the user types a model ID.

Verification happens when a key or model is added: a small real request including a test tool call. Traits `tool_calling` and `streaming` are set from what the request proves, never declared. An invalid key or unreachable URL is reported then, with the reason. An agent refuses any model without verified `tool_calling`.

Assignment: a default model per agent at app level; an optional per-agent override in a project's settings. An override pointing at a model that no longer exists shows a clear message asking the user to choose another.

Bridge: one function turns a registry entry into LangChain's OpenAI-compatible chat model (base URL, key from the keystore, wire model ID, streaming on). It is the only way agents obtain an LLM.

Failure during work: rate limits are retried and rotated across the provider's keys. A key that is revoked or out of quota mid-turn stops the turn with a clear message and marks the provider `needs_attention`; there is no retry loop.

## 4. GitHub integration

- Auth mode `gh`: if the GitHub CLI is installed and logged in, the user enables it with one click. The token is read from `gh auth token` each time it is needed, never copied, so a new `gh` login is picked up automatically.
- Auth mode `token`: the user pastes a fine-grained personal access token; it is verified against the GitHub API and stored in the keystore.
- Git commands receive the token per command (through `GIT_ASKPASS`), never in a remote URL, so it is never written to `.git/config`. Error text from git is scrubbed before it is shown or stored.
- Stage 1 uses GitHub for: verifying the login, listing the user's repositories, cloning and fetching. PRs and issues come in stage 2.

## 5. Agent runtime

### One runtime, agent definition files

The runtime is written once and owns everything agents share: the loop, the turn queue and Stop, streaming, persistence, compaction, usage logging, limits, and (from stage 2) the permission check and audit records. Each agent is a definition file (`agents/definitions/<name>.md`, format in the roadmap) that states only what is special about it: role prompt, tools, chat on/off, schedules, who it may message, context hooks, limits. Adding an agent means adding a file. Tests include a throwaway second definition to prove this.

The loop is our own small LangGraph graph (model step, tool step, repeat until the model answers), not the prebuilt agent, so later stages can insert steps (permission check, compaction trigger) without rewriting it.

### Tools

- General tools follow Claude Code's names and argument shapes. Stage 1 ships Read, Glob and Grep.
- Every tool registers its risk class (roadmap). Stage 1 tools are all `read`.
- Repo file filter, applied inside every stage 1 tool: only git-tracked files; a denylist for secrets (`.env*`, `*.pem`, `*.key`, `id_rsa*`, credential files); paths are resolved and must stay inside the repo (no `..` or symlink escapes). From stage 2 the same filter defines what the sandbox mounts.
- Output caps: a capped result says it was cut and how to narrow the request. Everything a tool returns, errors included, goes back into the agent's context.

### Orchestrator definition (stage 1)

Tools: Read, Glob, Grep. Chat on. Role: the project-level assistant that knows the project; in stage 1 it answers questions about the repo. Its system context includes the project name, source, default branch and the repo's top-level layout.

### Turns

1. The user sends a message; it is stored and shown immediately.
2. If the session is idle, a turn starts as a background task; otherwise the message is queued (one turn at a time per session) and shown as queued.
3. Model output and tool steps stream to the browser over SSE as they happen; tool steps appear as collapsible items.
4. The final message, tool calls and results are stored; one `llm_calls` row is written per model call.
5. Stop cancels the running turn; partial output is kept and marked stopped.

Limits: a maximum number of steps per turn (default 25), configurable per agent and per project, including off.

Compaction: when the conversation nears the model's context window, older turns are summarized into the agent's working state. The UI keeps the full history.

Recovery: the browser reconnects to the SSE stream and catches up from the last event it saw. On startup, any turn left `running` by a stopped process is marked `interrupted`.

## 6. UI

React + Vite + TypeScript, Tailwind + shadcn/ui, React Router, TanStack Query. The navigation only shows pages that work in the current stage.

App level (reached from the home screen or the gear icon):
- Projects: list with source, status and last opened; open; add.
- Add project: local folder (local mode only; folder browser + typed path, offer `git init`) or GitHub (URL or repo picker).
- AI providers: providers with masked keys, add/remove keys, detect keys from environment, models with traits and status, verify, default model per agent.
- Integrations: GitHub (enable gh login or paste token; show who is logged in).
- Settings: data folder, run mode, backup.

Project level (project switcher at the top of the sidebar):
- Agents: the Orchestrator session. Streaming answers, collapsible tool steps, queued messages, Stop, interrupted turns marked.
- Settings: repo info, refresh, relink (local), per-agent model override, limits.

API: REST under `/api`, plus `GET /api/projects/{id}/sessions/{id}/stream` (SSE). Errors share one JSON shape (`code`, `message`, optional `detail`) that the UI turns into plain, actionable text.

## 7. Error handling

- No secret (key, token) appears in an error, log, URL or stored record; error text that may contain one is scrubbed.
- Provider problems are caught at verification time; mid-turn failures stop the turn with a clear message.
- Workspace problems (missing folder, failed clone, not a git repo) set a project status the UI explains, with the action that fixes it.
- Tool errors are returned to the agent as tool results.

## 8. Testing

Test what needs testing, at the cheapest level that proves it. The risky parts:
- Repo file filter: untracked files, secrets, `..` and symlink escapes, Windows path cases.
- Keystore and credential scrubbing: secrets never stored in the database or echoed in errors.
- Migrations and backups on real SQLite files; concurrent writes from the app and the checkpointer.
- Workspace operations on temporary git repos, including duplicates and missing folders.
- Runtime with a scripted fake chat model: tool loop, queue, Stop, limits, compaction, restart recovery, and a second throwaway agent definition.
- Verification against a fake OpenAI-compatible server (tool call works / fails / bad key).
- One end-to-end check through the UI with the fake model (add project, ask the Orchestrator, see a streamed answer with a tool step). A live test against a real provider runs only on request.

## Done when

- `uv tool install` of the package and `mlops-agents start` open a working app on Windows and Linux.
- A user can add Groq/Gemini (or Ollama), verify models, add a local or GitHub project, and get correct, streamed answers about the repo from the Orchestrator, with tool steps visible.
- Untracked and secret files are never readable by the agent.
- Adding a second agent requires only a new definition file.
