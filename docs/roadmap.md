# Roadmap and shared contracts

The product is built in stages. Each stage has its own design spec in `docs/superpowers/specs/`, then a plan, then the build. Every spec follows the contracts in this file. A spec that needs to change a contract changes it here first.

Background and decisions: `project_knowledge.md`. The app name is undecided; `mlops-agents` (package `mlops_agents`) is a placeholder defined in one place.

## Stages

| # | Stage | Delivers |
|---|---|---|
| 1 | Foundation + first agent | Workspace and projects, storage, AI providers registry, GitHub integration, app shell UI, shared agent runtime, Orchestrator chatting with read-only Read/Glob/Grep |
| 2 | Agent runtime, full | All agent definitions, Bash + Docker sandbox, Write/Edit, ListAgents/SendMessage, permission policy + bypass, git worktrees, PRs and issues, Inbox approvals. Audit log and event stream stored from here on |
| 3 | Onboarding + data access | Onboarding diagnosis, data connections, DuckDB data-command hooks, tracking adapters (MLflow, git/CI), Models / Data / Runs pages |
| 4 | Monitoring loop | Checks + scheduler, Data Quality, Model Performance, incidents, Alert Prioritization, Discord + email, Dashboard, shadow baseline recording and verdict buttons from the first check |
| 5 | Action agents | Retraining, Deployment & Rollback, train/deploy hooks, Deployments page |
| 6 | Pages over collected data | Stats, Activity, Knowledge, Usage |
| 7 | Developer benchmarks | ULB credit card fraud, synthetic drift, pipeline telemetry (outside the product) |

Rule: a store lands in the earliest stage that produces its data; its page may land later. No stage invents its own logging or storage outside these contracts.

## Run modes

- `local` (default): `mlops-agents start`. Binds to 127.0.0.1. Access token in the URL opened at start, exchanged for a session cookie. Local folders and GitHub repos. Keys in the OS keyring.
- `server`: `mlops-agents start --server`. Password login (`MLOPS_AGENTS_PASSWORD`). GitHub repos only. Keys in an encrypted file unlocked by `MLOPS_AGENTS_MASTER_KEY`. HTTPS from a reverse proxy.
- In both modes every API request needs a valid session, `Host`/`Origin` are checked, and state-changing requests carry a CSRF token. Mode rules are enforced by the API, not only the UI.

## IDs

Every stored entity has an ID of the form `<prefix>_<ULID>` (ULID: 26 characters, sortable by creation time). Prefixes are registered here:

| Prefix | Entity | Stage |
|---|---|---|
| `prj` | project | 1 |
| `prv` | AI provider | 1 |
| `ses` | agent session | 1 |
| `msg` | message | 1 |
| `trn` | agent turn | 1 |
| `llm` | LLM call (usage record) | 1 |
| `evt` | event | 2 |
| `aud` | audit record | 2 |
| `apr` | approval request | 2 |
| `inc` | incident | 4 |

Model registry entries use addresses, not IDs: `<provider-name>:<wire-model-id>`.

## Data folder layout

```
<data folder>/                 platformdirs user data dir (non-roaming: %LOCALAPPDATA% on Windows), overridable with MLOPS_AGENTS_HOME
  app.db                       projects, AI providers registry, integrations, app settings
  keys.enc                     server mode only
  backups/
  projects/<prj_...>/
    project.db                 sessions, messages, agent state, usage, and all later per-project stores
    repo/                      managed clone (GitHub projects only; never edited by the app)
    worktrees/                 one per agent task (stage 2)
    cache/                     data-command results (stage 3)
```

Nothing is written into the user's working files. Agent branches (stage 2) live in the repo's git data.

## SQLite rules

- Every connection (SQLAlchemy and LangGraph checkpointer): WAL mode, `busy_timeout` set, foreign keys on.
- Migrations: numbered SQL files per database kind (`app`, `project`), applied when a file is opened, after an automatic backup of that file.
- Backups use `VACUUM INTO`, never a file copy.
- Database files stay on a native filesystem (not network drives or Windows bind mounts into containers).

## Agent definitions

Each agent is one markdown file with YAML frontmatter, in the style of Claude Code subagents. The runtime is shared; the file only says what is special about the agent.

```markdown
---
name: orchestrator            # unique, kebab-case
display_name: Orchestrator
description: One line shown in the UI and to other agents
tools: [Read, Glob, Grep]     # names from the tool registry
chat: true                    # false = background only (Alert Prioritization)
schedules: false
can_message: []               # agent names this agent may SendMessage to (stage 2)
context_hooks: []             # hook names run at session/turn start
limits: { max_steps: 25 }     # optional; null = unlimited
---
Role prompt.
```

## Tools

- General work uses Claude Code's standard tools and argument shapes: Read, Write, Edit, Glob, Grep, Bash.
- Specialized tasks get specialized tools (ListAgents, SendMessage, PR/issue tools, data commands).
- Bash only ever runs inside the Docker sandbox (stage 2).
- Every tool belongs to one risk class (below). The permission policy is enforced by the runtime, never by prompts.

## Permission risk classes

| Class | Examples |
|---|---|
| `read` | Read, Glob, Grep, ListAgents, data commands |
| `write_code` | Write, Edit (in a worktree) |
| `run` | Bash in the sandbox |
| `message` | SendMessage |
| `github` | open PR, open issue, comment |
| `deploy` | train / deploy / rollback hooks |

Policy: risk class x permission level (`suggest`, `ask`, `autonomous`, `bypass`) -> `allow` / `ask` / `deny`. The table of values is defined in the stage 2 spec.

## Event and audit records (stage 2)

Event (status board and live updates):
```
id evt_..., project_id, ts (UTC ISO 8601), source (agent name | "user" | "system"),
type (dotted, e.g. "turn.started", "pr.opened"), subject_id (optional), payload (JSON)
```
Audit record (permanent trail of decisions and actions):
```
id aud_..., project_id, ts, actor (agent name | "user"), action, risk_class,
permission_level, decision (allowed | asked | denied | approved | rejected), target, detail (JSON)
```

## Time

All timestamps are stored in UTC ISO 8601 and shown in the user's local time.
