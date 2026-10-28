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
- Git: one git worktree per agent task, under `projects/<id>/worktrees/`. Branch name `agent/<agent>/<task>`. File claims, enforced by the runtime, keep two agents from changing the same files at once. Agents never push to the default branch; at `bypass` they may merge their own PRs through the platform under the merge rules below (stage 5).
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

Answered in the stage 2 spec (`docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md`): the policy values, mid-turn delivery, scheduled work in the agent's one session, the sandbox environment and its cache, and the Inbox scope (approvals, local reviews, agents' PRs and issues).

### Stage 3: Onboarding and data access

Agreed:
- Full add-project wizard, ending in a session with the Onboarding agent.
- Onboarding sets the app up for the project and hands off: it reads the repo, asks about the model's objectives and use cases, and proposes the data connections, then the handoff (profile, table roles, tracking, pipeline stages, the team's commands, its scheduler and existing monitoring, gaps, grants). The user approves both. It also:
  - suggests data connections it finds in the code (file paths, `read_sql` queries, DVC files, MLflow URIs);
  - sets up tracking (connects MLflow, or opens a PR adding it);
  - finds the project's pipeline stages, which the Dashboard draws in stage 4.
- After the handoff, Data Quality, Model Performance, Retraining and Deployment & Rollback audit their part and propose fixes; checks are proposed by them (stage 4), not by Onboarding.
- Data connections: read-only, managed like a registry, credentials in the keystore. Stage 3 ships one kind, local folder; every other kind (PostgreSQL first, then S3, GCS, Azure, MySQL, ...) is one file implementing the same interface. MLflow is a tracking source, not a connection kind.
- Missing predictions or labels: an agent opens a PR that adds logging of predictions and labels, with a shared ID, to a location a connection can read.
- Agents never load whole datasets. They explore data with Bash in the sandbox (DuckDB, pandas) on the connections mounted read-only, with Bash's output cap. The app adds a capped summary of each table (the `data_summary` context hook, on DuckDB, querying files in place):
  - sample rows capped at 20; wide results truncated;
  - results (never data) are cached per data version (file size and mtime, or row count and latest timestamp), with a size cap per project (about 500 MB) and least-recently-used eviction.
- Tracking: integrate, never rebuild MLflow. One unified record (models, versions, runs, metrics over time, deployments, CI runs) is filled by adapters: MLflow and GitHub Actions runs. Lineage comes from the git commit MLflow records on each run. Other trackers (for example W&B) come later as adapters.
- Pages:
  - Models: versions, metrics across releases, comparisons, lineage (data version + commit).
  - Data: connections, data versions, quality and drift history.
  - Runs: pipeline and job runs, durations, failures, retries.
- Knowledge store: agents record what they learn here. sqlite-vec + FTS5 hybrid search inside `project.db`, behind a VectorStore interface. Code is read with file tools, not RAG. The embedding model comes from the AI providers registry (embedder entries, any provider).

Answered in the stage 3 spec (`docs/superpowers/specs/2026-10-27-stage-3-onboarding-data-design.md`): DuckDB per connection kind, the summary and its caps (no data-command tool), the tracking data model and the MLflow mapping, how proposals are stored and approved, and what the Knowledge store holds.

### Stage 4: Monitoring loop

Agreed (spec: `docs/superpowers/specs/2026-10-28-stage-4-monitoring-design.md`, approved):
- The app watches at any hour and is the first to know. It runs continuously and catches up after downtime without missing or repeating anything.
- Checks are deterministic code written by agents and delivered to the team's repository by PR; the team's own scheduler or CI runs them, never the app. The app reads every result through sources and parsers into one standard result record; a feed or job that misses its expected time is a signal too (silence).
- Signals are grouped by cause (and down the pipeline graph) into incidents with a lifecycle; the matching agent diagnoses and picks from the response menu, safest first, within the three tiers, always through the team's own commands (action hooks). Pre-approved rules are evaluated in code by the gate.
- Data Quality keeps validation and drift, classifies bad data against real drift, and explains every flag. Model Performance uses metrics suited to the problem and the error costs, never accuracy alone, and accounts for label delay.
- Severity and routing are deterministic code: paging never waits for a model. Alert Prioritization explains, merges and adjusts by one level. Discord and email; critical wakes the on-call person at any hour and escalates; a morning report. A heartbeat route lets the team's scheduler notice the app being down.
- Dashboard (one screen, each card linking to its page): pipeline map, current model and metrics, open incidents, expectations at risk, agent status.
- Baseline: the team's existing monitoring, read and recorded silently from the first result, never re-run and never notifying. Without readable existing monitoring there is no baseline, and Stats says so.
- Verdicts on every incident (real issue / not an issue / wrong type), user-reported misses, automatic confirmation by labels. Verdicts cannot be backfilled, so they ship here.

### Stage 5: Training, evaluation and promotion

Agreed (spec: `docs/superpowers/specs/2026-10-28-stage-5-action-agents-design.md`, approved):
- One system, the team's: agents run the team's own pipelines and improve any part of them through PRs, continuously (Agents and the team's system, below). The app has no training, evaluation or deploy path of its own.
- A diagnosing agent requests a fix; Retraining chooses it (re-threshold, recalibrate, retrain, or decline) and runs the team's pipeline through parameterised hooks (`command`, `ci_dispatch` on the default branch only), linked back to the tracker by a request ID.
- Training policies let the team's training run without asking within limits the user approved; a pipeline that deploys at its end (full CD) runs the same way only where the team's gate is the promotion authority and the model has a production signal the app reads; a `deploys: false` run that moved production suspends the hook.
- Evaluation lives in the team's pipeline and writes one standard evaluation record (parity, champion against challenger on the same production inputs, a time-based holdout, per-group metrics with intervals, calibration, cost at the threshold, serving compatibility, the gate's decision); records are compared only when made by the same code.
- Promotion follows the team's mechanism: their gate (full CD) or a person (or the agent at `bypass`), after a complete, fresh report. Gradual rollout and shadow traffic are the team's deployment's, added by PR where missing; reverting is stage 4's rollback rule.
- Deployments page: which version runs in each environment, promotions and rollbacks with who decided, each report.

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
- In both modes every API request needs a valid session, `Host`/`Origin` are checked, and state-changing requests carry a CSRF token. Mode rules are enforced by the API, not only the UI. One exception (stage 4): `GET /api/health/heartbeat`, which carries no data, takes no input and changes nothing, is answered without a session and without the Origin check; the Host check accepts the configured `public_url` host.

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
| `tsk` | agent task (worktree and branch) | 2 |
| `xch` | agent-to-agent exchange | 2 |
| `wak` | scheduled wake-up | 2 |
| `con` | data connection | 3 |
| `trk` | tracking source | 3 |
| `prp` | proposal | 3 |
| `kno` | knowledge note | 3 |
| `fix` | fix request | 5 |
| `trq` | training request | 5 |
| `pol` | training policy | 5 |
| `rep` | evaluation report | 5 |
| `prm` | promotion | 5 |
| `res` | result record | 4 |
| `sig` | signal | 4 |
| `rul` | pre-approved rule | 4 |
| `hok` | action hook | 4 |
| `win` | maintenance window | 4 |
| `chk` | registered check | 4 |
| `inc` | incident | 4 |

Model registry entries use addresses, not IDs: `<provider-name>:<wire-model-id>`. Tracking record rows mirror another system's IDs and are keyed by their tracking source and that ID, with no prefix.

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
    cache/                     data-summary results (stage 3)
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
context_hooks: []             # hook names from the hook registry, run at turn start (stage 3: status_board, data_summary, tracking_summary, knowledge)
limits: { max_steps: 25 }     # optional; null = unlimited
---
Role prompt.
```

## Tools

- General work uses Claude Code's standard tools and argument shapes: Read, Write, Edit, Glob, Grep, Bash.
- Specialized tasks get specialized tools (ListAgents, SendMessage, PR/issue tools, SaveNote, SearchNotes, TrackingHistory, Propose), only where Bash cannot do the job: the tool reads or writes the app's own database, needs a secret the sandbox must not hold, or needs its own approval. Data exploration is Bash in the sandbox.
- Bash only ever runs inside the Docker sandbox (stage 2).
- Every tool belongs to one risk class (below). The permission policy is enforced by the runtime, never by prompts.

## Permission risk classes

| Class | Examples |
|---|---|
| `read` | Read, Glob, Grep, ListAgents, SearchIssues, SaveNote, SearchNotes, TrackingHistory |
| `write_code` | Write, Edit (in a worktree), StartTask, ResumeTask |
| `run` | Bash in the sandbox |
| `message` | SendMessage, ScheduleWakeup |
| `github` | OpenPullRequest, OpenIssue, CommentOnIssue |
| `deploy` | action hooks: the team's train, evaluate, deploy, rollback, rerun, hold and fall-back commands; MergePullRequest |
| `propose` | Propose (connections, handoff; stage 4: parsers, rules, action hooks, maintenance windows). Checks are delivered by PR (`github`). |

Policy: risk class x permission level -> `allow` / `ask` / `deny` (stage 2 spec §5):

| Level | read | write_code | run | message | github | deploy | propose |
|---|---|---|---|---|---|---|---|
| suggest | allow | deny | deny | allow | ask | deny | ask |
| ask | allow | ask | ask | allow | ask | ask | ask |
| autonomous | allow | allow | allow | allow | allow | ask | ask |
| bypass | allow | allow | allow | allow | allow | allow | ask |

`propose` is the one class `bypass` does not skip: a proposal changes what agents may see or do, so the user always decides it.

`deploy` exceptions (stages 4 and 5). For an action hook the gate also receives the hook's kind, the rule or training policy it cites, and the hook's verified `deploys` and production signal, and evaluates them in code:

| Action | Within an approved rule or training policy | Otherwise |
|---|---|---|
| promotion (deploy hook), MergePullRequest | as the `deploy` column: denied at `suggest`, ask at `ask` and `autonomous`, allow at `bypass` | same |
| train, evaluate | allow at `ask` and above | ask |
| a pipeline run that deploys at its end, where the team's gate is the promotion authority and the model has a production signal | allow at `ask` and above | ask |
| rollback, rerun, fall back | allow at `ask` and above; deny at `suggest` | the `deploy` column |
| hold | allow at every level except `suggest`, where the person applies it in one click | same |

A PR that touches a protection surface (the promotion gate, the evaluation step's decision logic, the workflows behind required checks, `CODEOWNERS`) is never merged by an agent, at any level.

### Hard limits

At every level, `bypass` included, enforced in code:
- nothing runs outside the sandbox;
- connections are read-only, and nothing outside approved folders is read;
- secrets never reach the sandbox, code, commits, PRs, logs or a model's context (a hook's credential reaches only that hook's run);
- no push to the default branch, force-push or history rewrite; merging only as above, only when the platform reports the PR clean, never with an admin override, with at least one passing check;
- no unmerged code runs with the team's secrets;
- the team's protections (branch protection, required reviews, the team's gate) are never overridden;
- agents never give themselves more access (folders, network, credentials, a higher level, rules, policies): always the user's click;
- the audit log records everything and cannot be turned off;
- the team's history is never destroyed: no deleting model versions, runs, tables or others' branches, no closing the team's PRs or issues by hand (an issue closed by a merged agent PR that says `Fixes #n` is expected);
- text in data, results and logs is data, never instructions;
- no promotion without a complete, fresh evaluation report made by the same code;
- no contact outside the configured channels and people.

### Agents and the team's system

There is one system, the team's. Agents change any part of it through PRs, continuously, whenever a need appears, with this discipline: search open PRs (the team's included) and update a matching agent PR instead of duplicating; one concern per PR, owned by the agent whose area it is; a cap on open improvement PRs (fixes for open incidents exempt); PRs that change decision semantics are labeled `decision-semantics` and state the behavior before and after; stale or conflicting agent PRs are updated or closed with a note; a PR closed unmerged becomes a Knowledge note with its reason; after an incident, recent changes (the team's and the agents') are linked to it, and agents may revert their own.

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
A stalled turn (every provider key exhausted, stage 3) pauses on an approval of kind `stall`; it is not a policy decision and writes no audit record (events record it).

## Time

All timestamps are stored in UTC ISO 8601 and shown in the user's local time.
