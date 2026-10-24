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

## Stage details

What each stage's spec must cover: the design points already agreed (a spec may refine them, not silently drop them) and the questions the spec must answer. Stage 1 is fully specified in `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`.

### Stage 2: Agent runtime, full

Agreed:
- Agent definitions for all seven agents on the stage 1 runtime:
  - Orchestrator: the project-level assistant, like the assistant in a claude.ai Project. It knows the whole project and does not sit between the user and other agents.
  - Onboarding & Diagnosis.
  - Data Quality: validation and drift combined. It decides first whether a change is bad data (block it) or real drift (inform adaptation).
  - Model Performance.
  - Retraining.
  - Deployment & Rollback.
  - Alert Prioritization: `chat: false`, workflow-like, background only.
  Every agent except Alert Prioritization is a full coding agent.
- Sessions: one per agent per project, between the user and that agent.
- Agent messaging tools, modeled on Claude Code: `ListAgents` (agents and their status, read from the status board) and `SendMessage(to, message)`. The message lands in the recipient's session labeled with the sender; the reply lands back in the sender's session labeled with the replier. The sender does not block; the reply arrives as a new turn. Safeguards: hop limit and token budget per exchange; no permission escalation (the recipient's actions still go through the policy); every exchange visible in Activity and on the status board.
- Status board: every agent publishes what it is doing, what it finished, PRs it opened and what it waits on (from the event stream). A summary is injected into each Orchestrator turn; details on request.
- Tools: Write and Edit (inside the task's worktree), Bash (only inside the sandbox), ListAgents, SendMessage, PR and issue tools.
- Docker sandbox: Linux container per agent task. It mounts a filtered view of the worktree (git-tracked files minus the secret denylist). Network access is off by default. Read, Glob and Grep see the same view.
- Git: one git worktree per agent task, under `projects/<id>/worktrees/`. Branch name `agent/<agent>/<task>`. The Orchestrator prevents two agents from changing the same files at once. Agents never edit the default branch.
- PRs: at the end of each turn the runtime reminds the agent when its work may need a PR; the agent decides and opens it through GitHub on the user's behalf. The PR body says which agent opened it and why. PR identity is the user's choice: their own token, or later a bot (GitHub App).
- Issues: an agent opens an issue when a human must act and a PR when it has a fix. A PR that fixes an issue says `Fixes #n`. Before opening, the agent searches open issues and comments on a match instead. Only incidents above a configurable priority become issues. Labels: agent, category, priority.
- Local repo without a remote: agent branch; the diff is reviewed in the Inbox; approving merges it.
- Permission policy: risk class x level (`suggest`, `ask`, `autonomous`, `bypass`) -> allow / ask / deny, enforced by the runtime. Bypass skips approvals but keeps the sandbox and the audit log. It is off by default, set per project, and shown with a warning.
- Approvals: a turn that needs approval pauses as `waiting` (survives restarts) and resumes after the user decides. Approval requests appear in the session and in the Inbox (diffs for code changes).
- Audit log and event stream (shapes in this file) are stored from this stage on.
- Talking to an agent directly: the agent acts; the Orchestrator learns of it through the status board; the policy applies either way.
- A tool call that fails, whether our tool errors or the provider rejects the call, is input for the model, never the end of the turn.
- UI changes from the lead's stage 1 walkthrough:
  - Searchable pickers (repo, model, ...) use one input that both filters and holds the value.
  - Every select uses the app's own styled component, never the browser default.
  - Focus and hover rings follow each element's corner radius.
  - Every clickable element shows the pointer cursor.
  - Provider cards have no empty vertical space.
  - The context window is read from the provider where it reports one.
  - Model warnings sit above the models table, with a colored marker on the row; rows keep one height.
  - Chat follows the usual chat-app layout:
    - the input starts at one line, grows to a limit, then scrolls;
    - one button, vertically centred, sends and becomes Stop during a turn;
    - only the message list scrolls.
  - Markdown tables in messages have borders.
  - The project switcher in the sidebar header fits and reads well.
  - App pages keep the current project selected.

The spec must answer:
- The policy table values (risk class x level).
- What happens when a message from another agent arrives while the recipient is mid-turn (queue order, priority).
- Where scheduled and triggered work appears: inside the agent's session, or as separate run records linked from it.
- How the sandbox gets the user's environment (built from requirements / pyproject / conda files), and how it is cached.
- Inbox page scope in this stage (approvals only, or also PRs and issues).

### Stage 3: Onboarding and data access

Agreed:
- Full add-project wizard, ending in a session with the Onboarding agent.
- Onboarding: reads the repo, asks about the model's objectives and use cases, then proposes tests and a monitoring plan for the user to approve. It also:
  - suggests data connections it finds in the code (file paths, `read_sql` queries, DVC files, MLflow URIs);
  - sets up tracking (connects MLflow, or opens a PR adding it);
  - finds the project's pipeline stages, which the Dashboard draws in stage 4.
- Data connections: read-only. Kinds: local folder, S3/GCS/Azure, SQL database, MLflow. Managed like a registry. Credentials go in the keystore.
- Missing predictions or labels: an agent opens a PR that adds logging of predictions and labels, with a shared ID, to a location a connection can read.
- Agents never load whole datasets. Data understanding uses standard notebook commands (head, describe, value counts, top, ...) run as context hooks. Their output is injected into the agent's context.
- Hooks run on DuckDB, querying files and databases in place.
  - Output is capped (sample rows capped at about 20; wide results truncated).
  - A truncated result carries a handle. The agent can page through it (`read_result`) or run a narrower command.
  - Results (never data) are cached. The cache key is connection + table + command + arguments + data version (file size and mtime, S3 ETag, or row count and latest timestamp).
  - Each project's cache has a size cap (about 500 MB) with least-recently-used eviction.
  - Results are cached per data version, so a hook is not rerun every turn.
- Tracking: integrate, never rebuild MLflow. One unified record (models, versions, runs, data versions, deployments, metrics over time) is filled by adapters: MLflow first, git/CI (commits, tags, GitHub Actions runs), and the agents' own runs. Other trackers (for example W&B) come later as adapters.
- Pages:
  - Models: versions, metrics across releases, comparisons, lineage (data version + commit).
  - Data: connections, data versions, quality and drift history.
  - Runs: pipeline and job runs, durations, failures, retries.
- Knowledge store: agents record what they learn here. sqlite-vec + FTS5 hybrid search inside `project.db`, behind a VectorStore interface. Code is read with file tools, not RAG. The embedding model comes from the AI providers registry (embedder entries).

The spec must answer:
- Which DuckDB extensions cover each connection kind, and what to do where none does.
- The exact command set and their caps.
- The tracking data model and the MLflow adapter's mapping.
- How the Onboarding agent's proposed tests and monitoring plan are stored and approved.
- What the Knowledge store holds (notes, decisions, incident history, docs) and who writes what.

### Stage 4: Monitoring loop

Agreed:
- Checks are deterministic code written by agents and run by the scheduler. The LLM wakes only when a check fires. Schedules live in the app's own tables and are configurable on the Agents page.
- Data Quality keeps two test families, validation and drift. It classifies each change as bad data or real drift before acting, and explains every flag.
- Model Performance uses metrics suited to the problem. For imbalanced data: PR-AUC, recall at a fixed precision, per-class metrics, never accuracy alone. It accounts for label delay.
- Incidents have a category: data quality, drift, model performance, operational.
- Alert Prioritization deduplicates, ranks and routes alerts, and merges raw alerts that share a cause into one incident.
- Notifications: Discord and email (SMTP) in this stage. WhatsApp and Slack later.
- Operational telemetry: GitHub Actions run history first. Airflow, cron and other schedulers come in through data connections.
- Dashboard (one screen, each card linking to its page): pipeline map from Onboarding (stages are not fixed), current model version and metrics, deployment status, data quality, open incidents, agent status.
- Shadow baseline, recorded silently from the very first check and never notifying anyone. Naive static checks per category:
  - data quality: schema, nulls, ranges from training data;
  - drift: a drift test per feature at p < 0.05;
  - model performance: a fixed threshold on accuracy;
  - operational: fixed timeouts and failure counts.
  Configurable per project.
- Verdicts on every incident, one click: real issue / not an issue / wrong type. Also: user-reported misses, baseline alerts the agents stayed silent on that the user marks real, and automatic confirmation when labels prove a metric drop. Verdicts cannot be backfilled, so they ship here.

The spec must answer:
- The incident data model and lifecycle.
- How Alert Prioritization ranks (inputs and rules).
- The scheduler design inside the single process.
- How checks written by agents are stored, versioned and run (sandbox or not).

### Stage 5: Action agents

Agreed:
- Retraining decides when to retrain from drift and performance signals. It trains, then evaluates the new model against the current one in shadow, and asks for approval before promotion.
- Deployment & Rollback rolls out gradually (canary) behind monitoring gates and reverts automatically on degradation.
- Both act through hooks (train / evaluate / deploy / rollback commands) that agents find or write in the user's project. The system never assumes the user's infrastructure.
- Deployments page: which model version runs where, and the rollout and rollback history.

The spec must answer:
- Where hooks run: the sandbox, the host, or the user's own infrastructure (GPUs, cloud credentials, network access).
- The promotion and rollback gates and their thresholds.

### Stage 6: Pages over collected data

Agreed:
- Stats: the agents vs the baseline on the user's own project, per incident category and per time window. Metric groups:
  - detection: precision, catches the baseline missed, false alarms avoided, misses;
  - speed: detection delay, time to resolution;
  - noise: alerts per week, raw alerts merged per incident;
  - diagnosis: bad data vs drift correctness;
  - human effort: share of actions taken autonomously, approvals requested, acceptance rate, PRs opened / merged / closed;
  - model impact: metric before vs after agent actions;
  - cost: tokens and $ per agent, cost per incident, compute used by checks;
  - reliability: failed runs, tool errors, sandbox failures.
  Every number shows how many verdicts it rests on and stays "collecting data" until there are enough.
- Activity: the audit trail of every decision, action, approval and agent-to-agent message.
- Knowledge: what agents have recorded; the user can view, correct and delete entries.
- Usage (app level): LLM tokens and cost across projects, per agent, per week.

The spec must answer:
- Minimum verdict counts before a number is shown.
- How cost in $ is computed per provider (price data source).

### Stage 7: Developer benchmarks (outside the product)

Agreed:
- Run by the app's developers, never shown to users. It uses the same engine as Stats, with injected answer keys instead of user verdicts.
- Datasets:
  - ULB credit card fraud: extreme class imbalance (about 0.17% fraud);
  - a synthetic drift dataset, with known drift points;
  - a pipeline telemetry dataset, for operational incidents.

The spec must answer: which synthetic drift and telemetry datasets exactly, the injected scenarios, and the report format.

### Not assigned to a stage

Deferred:
- Local model files (GGUF via an app-managed llama.cpp server).
- WhatsApp and Slack notifications.
- GitHub App bot identity for agent PRs.
- Cloud secret managers.
- Master key rotation.
- Postgres deployment.
- Tracker adapters beyond MLflow.

Ideas, not adopted (see `project_knowledge.md`):
- An automation agent.
- A Resource/Cost Optimization agent.
- A Security/Compliance agent.

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
